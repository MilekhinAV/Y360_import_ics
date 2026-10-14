import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from icalendar import Calendar
import import_ics as app


def event(uid="test", extra="", start="20261013T090000Z"):
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20261001T000000Z\r\n"
            f"DTSTART:{start}\r\nSUMMARY:Проверка\r\n{extra}END:VEVENT\r\n")


def calendar(*events):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//RU\r\n"
            + "".join(events) + "END:VCALENDAR\r\n").encode()


class Tests(unittest.TestCase):
    def test_rejected_event_does_not_block_next(self):
        client = Mock()
        client.existing_uids.return_value = set()
        client.call.side_effect = [app.HttpFailure("PUT", 400, "valid-calendar-data"),
                                   (201, b""), (200, calendar(event("next")))]
        with tempfile.TemporaryDirectory() as folder, patch("sys.stdout", new_callable=io.StringIO):
            report = Path(folder) / "r.csv"
            result = app.import_events(client, app.BASE, {"test": calendar(event()), "next": calendar(event("next"))}, report)
            with report.open(encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f, delimiter=";"))
        self.assertEqual(result, 1)
        self.assertEqual([r["status"] for r in rows], ["rejected", "created"])
        self.assertEqual(rows[0]["error"], "PUT:http_400:valid-calendar-data")

    def test_only_put_validation_errors_allow_continuation(self):
        for method, status in [("PUT", 401), ("PUT", 403), ("PUT", 412), ("GET", 400)]:
            with self.subTest(method=method, status=status), tempfile.TemporaryDirectory() as folder, patch("sys.stdout", new_callable=io.StringIO):
                client = Mock()
                client.existing_uids.return_value = set()
                client.call.side_effect = app.HttpFailure(method, status)
                self.assertEqual(app.import_events(client, app.BASE, {"test": calendar(event()), "next": calendar(event("next"))}, Path(folder) / "r.csv"), 1)
                self.assertEqual(client.call.call_count, 1)

    def test_caldav_error_diagnostics_do_not_leak_text(self):
        body = b'<d:error xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><c:valid-calendar-data>secret</c:valid-calendar-data><d:responsedescription>secret</d:responsedescription><c:secret/></d:error>'
        self.assertEqual(app.safe_caldav_error(io.BytesIO(body)), "valid-calendar-data")
        for body in [b'<html>secret</html>', b'{"error":"secret"}', b'<!DOCTYPE x><x/>', b'<x:secret xmlns:x="secret"/>']:
            self.assertNotIn("secret", app.safe_caldav_error(io.BytesIO(body)))

    def test_transport_extracts_caldav_error_and_closes_response(self):
        body = b'<d:error xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><c:valid-calendar-object-resource/></d:error>'
        error = HTTPError(app.BASE, 400, "secret", {}, io.BytesIO(body))
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(app, "build_opener", return_value=opener), patch.object(app.PACER, "wait"):
            with self.assertRaises(app.HttpFailure) as caught:
                app.transport_once("PUT", app.BASE)
        self.assertEqual(str(caught.exception), "PUT:http_400:valid-calendar-object-resource")
        self.assertTrue(error.closed)

    def test_404_with_diagnostic_still_finds_uid(self):
        client = Mock()
        client.call.side_effect = app.HttpFailure("GET", 404, "caldav_error_empty")
        client.find_event.return_value = calendar(event())
        self.assertEqual(app.read_created_event(client, app.BASE, app.BASE + "x", "test"), calendar(event()))

    def test_report_details_series(self):
        body = calendar(event(extra="RRULE:FREQ=WEEKLY;COUNT=3\r\n"),
                        event(extra="RECURRENCE-ID:20261020T090000Z\r\n"))
        row = app.report_details("test", body, {"test": {"b.ics", "a.ics"}})
        self.assertEqual(row["summary"], "Проверка")
        self.assertEqual(row["start"], "2026-10-13T09:00:00+00:00")
        self.assertEqual(row["exception_count"], 1)
        self.assertEqual(row["source_files"], '["a.ics", "b.ics"]')

    def test_report_all_day_duration_and_formula(self):
        body = calendar(event(extra="DURATION:P1D\r\n")).replace(b"DTSTART:20261013T090000Z", b"DTSTART;VALUE=DATE:20261013")
        row = app.report_details("test", body)
        self.assertTrue(row["all_day"])
        self.assertEqual(row["start"], "2026-10-13")
        self.assertEqual(row["end"], "")
        self.assertEqual(row["duration"], "P1D")
        self.assertEqual(app.csv_safe({"summary": "=1+1"})["summary"], "'=1+1")

    def test_prepare_records_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "source.ics").write_bytes(calendar(event()))
            sources = {}
            app.prepare(Path(folder), sources)
            self.assertEqual(sources, {"test": {"source.ics"}})

    def test_temporary_errors_retry_only_reads(self):
        for method in ("GET", "REPORT", "PROPFIND"):
            for status in (502, 503, 504):
                with self.subTest(method=method, status=status), patch.object(app, "transport_once", side_effect=[app.TemporaryServerError(method, status), (200, b"ok")]) as send, patch.object(app.time, "sleep") as sleep, patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(app.transport(method, app.BASE), (200, b"ok"))
                    self.assertEqual(send.call_count, 2)
                    sleep.assert_called_once_with(5)

    def test_temporary_errors_do_not_retry_writes(self):
        for method in ("PUT", "POST", "DELETE"):
            with self.subTest(method=method), patch.object(app, "transport_once", side_effect=app.TemporaryServerError(method, 504)) as send, patch.object(app.time, "sleep") as sleep:
                with self.assertRaises(app.TemporaryServerError):
                    app.transport(method, app.BASE)
                self.assertEqual(send.call_count, 1)
                sleep.assert_not_called()

    def test_read_retry_budget_shared_with_429(self):
        errors = [app.TemporaryServerError("GET", 502), app.RateLimited("GET", 40),
                  app.TemporaryServerError("GET", 503), app.TemporaryServerError("GET", 504)]
        with patch.object(app, "transport_once", side_effect=errors) as send, patch.object(app.time, "sleep") as sleep, patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaisesRegex(app.Failure, "GET:http_504"):
                app.transport("GET", app.BASE)
        self.assertEqual(send.call_count, 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 40, 20])

    def test_http_503_preserves_retry_after(self):
        opener = Mock()
        opener.open.side_effect = HTTPError(app.BASE, 503, "private", {"Retry-After": "90"}, io.BytesIO())
        with patch.object(app, "build_opener", return_value=opener), patch.object(app.PACER, "wait"), patch.object(app.time, "sleep") as sleep, patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaisesRegex(app.Failure, "GET:http_503"):
                app.transport("GET", app.BASE)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [90, 90, 90])

    def test_get_504_after_put_never_repeats_put(self):
        client = app.CalDAV({})
        client.token, client.expires = "test", float("inf")
        body = calendar(event())
        with patch.object(app, "transport_once", side_effect=[(201, b""), app.TemporaryServerError("GET", 504), (200, body)]) as send, patch.object(app.time, "sleep"), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(app.create_and_read(client, app.BASE, app.BASE + "x.ics", "test", body), body)
        self.assertEqual([c.args[0] for c in send.call_args_list], ["PUT", "GET", "GET"])

    def test_pacer_waits_between_requests(self):
        pacer = app.RequestPacer(2)
        with patch.object(app.time, "monotonic", side_effect=[10, 10, 10.5, 12]), patch.object(app.time, "sleep") as sleep:
            pacer.wait()
            pacer.wait()
        sleep.assert_called_once_with(1.5)

    def test_retry_after_parsing(self):
        self.assertEqual(app.retry_after_seconds("120"), 120)
        self.assertEqual(app.retry_after_seconds("Thu, 01 Jan 1970 00:00:00 GMT"), 0)
        self.assertIsNone(app.retry_after_seconds("invalid"))

    def test_429_header_preserved(self):
        error = HTTPError(app.BASE, 429, "limit", {"Retry-After": "75"}, io.BytesIO())
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(app, "build_opener", return_value=opener), patch.object(app.PACER, "wait"):
            with self.assertRaises(app.RateLimited) as caught:
                app.transport_once("PUT", app.BASE)
        self.assertEqual(caught.exception.retry_after, 75)

    def test_read_429_retries_and_honors_delay(self):
        with patch.object(app, "transport_once", side_effect=[app.RateLimited("REPORT", 75), (207, b"ok")]) as send, patch.object(app.time, "sleep") as sleep, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(app.transport("REPORT", app.BASE), (207, b"ok"))
        sleep.assert_called_once_with(75)
        self.assertEqual(send.call_count, 2)

    def test_put_429_checks_uid_before_retry(self):
        client = Mock()
        client.call.side_effect = [app.RateLimited("PUT", 5), (201, b""), (200, calendar(event()))]
        client.find_event.return_value = None
        with patch.object(app.time, "sleep") as sleep, patch("sys.stdout", new_callable=io.StringIO):
            result = app.create_and_read(client, app.BASE, app.BASE + "x.ics", "test", calendar(event()))
        self.assertEqual(result, calendar(event()))
        self.assertEqual([call[0] for call in client.method_calls], ["call", "find_event", "call", "call"])
        sleep.assert_called_once_with(5)

    def test_put_429_found_uid_not_written_again(self):
        client = Mock()
        client.call.side_effect = app.RateLimited("PUT")
        client.find_event.return_value = calendar(event())
        with patch.object(app.time, "sleep"), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(app.create_and_read(client, app.BASE, app.BASE + "x.ics", "test", calendar(event())), calendar(event()))
        self.assertEqual(client.call.call_count, 1)

    def test_put_429_retry_budget(self):
        client = Mock()
        client.call.side_effect = app.RateLimited("PUT")
        client.find_event.return_value = None
        with patch.object(app.time, "sleep") as sleep, patch("sys.stdout", new_callable=io.StringIO), self.assertRaises(app.RateLimited):
            app.create_and_read(client, app.BASE, app.BASE + "x.ics", "test", calendar(event()))
        self.assertEqual(client.call.call_count, 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [30, 60, 120])

    def prepare(self, *bodies):
        with tempfile.TemporaryDirectory() as folder:
            for i, body in enumerate(bodies):
                Path(folder, f"{i}.ics").write_bytes(body)
            return app.prepare(Path(folder))

    def test_split_and_series_across_files(self):
        master = event(extra="RRULE:FREQ=WEEKLY;COUNT=3\r\nEXDATE:20261027T090000Z\r\n")
        exception = event(extra="RECURRENCE-ID:20261020T090000Z\r\n", start="20261020T100000Z")
        prepared, files, count = self.prepare(calendar(master, event("second")), calendar(exception, master))
        self.assertEqual((len(prepared), files, count), (2, 2, 4))
        series = Calendar.from_ical(prepared["test"]).walk("VEVENT")
        self.assertEqual(len(series), 2)
        self.assertTrue(any("RRULE" in e and "EXDATE" in e for e in series))
        self.assertTrue(any("RECURRENCE-ID" in e for e in series))

    def test_no_scheduling_or_alarms(self):
        extra = ("ATTENDEE:mailto:guest@example.org\r\nORGANIZER:mailto:owner@example.org\r\n"
                 "X-TELEMOST-REQUIRED:TRUE\r\nBEGIN:VALARM\r\nACTION:EMAIL\r\n"
                 "TRIGGER:-PT10M\r\nEND:VALARM\r\n")
        body = calendar(event(extra=extra)).replace(b"VERSION:2.0", b"VERSION:2.0\r\nMETHOD:REQUEST")
        prepared, _, _ = self.prepare(body)
        for keyword in (b"METHOD:", b"ATTENDEE:", b"ORGANIZER:", b"VALARM", b"X-TELEMOST-REQUIRED"):
            self.assertNotIn(keyword, prepared["test"])

    def test_all_day_and_timezone_preserved(self):
        body = calendar(event()).replace(b"DTSTART:20261013T090000Z", b"DTSTART;VALUE=DATE:20261013")
        prepared, _, _ = self.prepare(body)
        self.assertIn(b"DTSTART;VALUE=DATE:20261013", prepared["test"])
        zone = ("BEGIN:VTIMEZONE\r\nTZID:Europe/Moscow\r\nBEGIN:STANDARD\r\n"
                "DTSTART:19700101T000000\r\nTZOFFSETFROM:+0300\r\nTZOFFSETTO:+0300\r\n"
                "END:STANDARD\r\nEND:VTIMEZONE\r\n")
        body = calendar(zone, event()).replace(b"DTSTART:20261013T090000Z", b"DTSTART;TZID=Europe/Moscow:20261013T120000")
        prepared, _, _ = self.prepare(body)
        self.assertIn(b"BEGIN:VTIMEZONE", prepared["test"])
        self.assertIn(b"DTSTART;TZID=Europe/Moscow:20261013T120000", prepared["test"])

    def test_conflicting_uid_fails_before_import(self):
        with self.assertRaisesRegex(app.Failure, "conflicting_uid"):
            self.prepare(calendar(event()), calendar(event(start="20261014T090000Z")))

    def test_orphan_and_invalid_file(self):
        with self.assertRaisesRegex(app.Failure, "orphan_recurrence"):
            self.prepare(calendar(event(extra="RECURRENCE-ID:20261013T080000Z\r\n")))
        with self.assertRaises(app.Failure):
            self.prepare(b"not an ics")

    def test_existing_uid_never_written(self):
        client = Mock()
        client.existing_uids.return_value = {"test"}
        with tempfile.TemporaryDirectory() as folder, patch("sys.stdout", new_callable=io.StringIO):
            code = app.import_events(client, app.BASE, {"test": calendar(event())}, Path(folder) / "r.csv")
        self.assertEqual(code, 0)
        client.call.assert_not_called()

    def test_create_conditional_and_verified(self):
        client = Mock()
        client.existing_uids.return_value = set()
        body = calendar(event())
        client.call.side_effect = [(201, b""), (200, body)]
        with tempfile.TemporaryDirectory() as folder, patch("sys.stdout", new_callable=io.StringIO):
            report = Path(folder) / "r.csv"
            code = app.import_events(client, app.BASE, {"test": body}, report)
            with report.open(encoding="utf-8-sig") as stream:
                row, = list(csv.DictReader(stream, delimiter=";"))
        self.assertEqual(code, 0)
        self.assertEqual(row["status"], "created")
        self.assertEqual(client.call.call_args_list[0].args[3]["If-None-Match"], "*")
        self.assertEqual(client.call.call_args_list[1].args[0], "GET")

    def test_unknown_write_stops_without_retry(self):
        client = Mock()
        client.existing_uids.return_value = set()
        client.call.side_effect = app.Failure("PUT:network_error")
        with tempfile.TemporaryDirectory() as folder, patch("sys.stdout", new_callable=io.StringIO):
            code = app.import_events(client, app.BASE, {
                "test": calendar(event()), "other": calendar(event("other")),
            }, Path(folder) / "r.csv")
        self.assertEqual(code, 1)
        self.assertEqual(client.call.call_count, 1)

    def test_get_404_uses_uid_and_continues_import(self):
        client = Mock()
        client.existing_uids.return_value = set()
        first, second = calendar(event()), calendar(event("other"))
        client.call.side_effect = [(201, b""), app.Failure("GET:http_404"),
                                   (201, b""), app.Failure("GET:http_404")]
        client.find_event.side_effect = [first, second]
        with tempfile.TemporaryDirectory() as folder, patch("sys.stdout", new_callable=io.StringIO):
            report = Path(folder) / "r.csv"
            self.assertEqual(app.import_events(client, app.BASE, {"test": first, "other": second}, report), 0)
            with report.open(encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream, delimiter=";"))
        self.assertEqual([r["status"] for r in rows], ["created", "created"])
        self.assertEqual(sum(c.args[0] == "PUT" for c in client.call.call_args_list), 2)

    def test_verification_retry_is_read_only(self):
        client = Mock()
        client.call.side_effect = app.Failure("GET:http_404")
        client.find_event.side_effect = [None, calendar(event())]
        with patch.object(app.time, "sleep"):
            self.assertEqual(app.read_created_event(client, app.BASE, app.BASE + "x.ics", "test"), calendar(event()))
        self.assertEqual(client.call.call_count, 1)
        self.assertEqual(client.find_event.call_count, 2)

    def test_missing_uid_still_fails(self):
        client = Mock()
        client.call.side_effect = app.Failure("GET:http_404")
        client.find_event.return_value = None
        with patch.object(app.time, "sleep"), self.assertRaisesRegex(app.Failure, "verification_uid_not_found"):
            app.read_created_event(client, app.BASE, app.BASE + "x.ics", "test")

    def test_report_verification_preserves_recurrence_check(self):
        client = Mock()
        client.existing_uids.return_value = set()
        client.call.side_effect = [(201, b""), app.Failure("GET:http_404")]
        client.find_event.return_value = calendar(event())
        series = calendar(event(), event(extra="RECURRENCE-ID:20261020T090000Z\r\n"))
        with tempfile.TemporaryDirectory() as folder, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(app.import_events(client, app.BASE, {"test": series}, Path(folder) / "r.csv"), 1)

    def test_find_event_matches_exact_uid_and_escapes_xml(self):
        uid = "test&<uid>"
        root = app.ET.Element(app.D + "multistatus")
        response = app.ET.SubElement(root, app.D + "response")
        app.ET.SubElement(response, app.D + "href").text = "/server-name.ics"
        propstat = app.ET.SubElement(response, app.D + "propstat")
        app.ET.SubElement(propstat, app.D + "status").text = "HTTP/1.1 200 OK"
        prop = app.ET.SubElement(propstat, app.D + "prop")
        app.ET.SubElement(prop, app.C + "calendar-data").text = calendar(event(uid)).decode()
        client = app.CalDAV({})
        client.call = Mock(return_value=(207, app.ET.tostring(root)))
        result = client.find_event(app.BASE, uid)
        self.assertEqual(str(Calendar.from_ical(result).walk("VEVENT")[0]["UID"]), uid)
        request = app.ET.fromstring(client.call.call_args.args[2])
        self.assertEqual(request.find(".//" + app.C + "text-match").text, uid)
        self.assertIsNone(client.find_event(app.BASE, "other"))

    def test_missing_exception_detected_on_verification(self):
        client = Mock()
        client.existing_uids.return_value = set()
        client.call.side_effect = [(201, b""), (200, calendar(event()))]
        body = calendar(event(), event(extra="RECURRENCE-ID:20261020T090000Z\r\n"))
        with tempfile.TemporaryDirectory() as folder, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(app.import_events(client, app.BASE, {"test": body}, Path(folder) / "r.csv"), 1)

    def test_xml_and_other_hosts_rejected(self):
        for url in ("https://other.example/", "http://caldav.yandex.ru/", "https://caldav.yandex.ru@other.example/"):
            with self.assertRaises(app.Failure):
                app.safe_url(url)
        with self.assertRaises(app.Failure):
            app.xml(b'<!DOCTYPE a [<!ENTITY x "test">]><a>&x;</a>')

    def test_caldav_discovery(self):
        def response(href, prop):
            return (f'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                    f'<d:response><d:href>{href}</d:href><d:propstat><d:prop>{prop}</d:prop>'
                    '<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>').encode()
        client = app.CalDAV({})
        client.call = Mock(side_effect=[
            (207, response('/', '<d:current-user-principal><d:href>/p/user/</d:href></d:current-user-principal>')),
            (207, response('/p/user/', '<c:calendar-home-set><d:href>/calendars/user/</d:href></c:calendar-home-set>')),
            (207, response('/calendars/user/test/', '<d:resourcetype><c:calendar/></d:resourcetype><d:displayname>Test</d:displayname>')),
        ])
        self.assertEqual(client.calendars(), {'https://caldav.yandex.ru/calendars/user/test/': 'Test'})

    def test_report_partial_results_fail_closed(self):
        client = app.CalDAV({})
        client.call = Mock(return_value=(207, b'<d:multistatus xmlns:d="DAV:"><d:response>'
                                       b'<d:href>/event.ics</d:href><d:status>HTTP/1.1 403 Forbidden</d:status>'
                                       b'</d:response></d:multistatus>'))
        with self.assertRaisesRegex(app.Failure, 'incomplete_uid_report'):
            client.existing_uids(app.BASE)

    def test_report_finds_uid_regardless_of_filename(self):
        client = app.CalDAV({})
        body = (b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                b'<d:response><d:href>/unrelated.ics</d:href><d:propstat><d:prop>'
                b'<c:calendar-data>BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:test\r\n'
                b'END:VEVENT\r\nEND:VCALENDAR\r\n</c:calendar-data></d:prop>'
                b'<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>')
        client.call = Mock(return_value=(207, body))
        self.assertEqual(client.existing_uids(app.BASE), {'test'})

    def test_oauth_secrets_not_in_error(self):
        error = HTTPError("https://oauth.yandex.ru/token", 400, "fake-secret", {},
                          io.BytesIO(b'{"error":"invalid_client","error_description":"fake-secret"}'))
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(app, "build_opener", return_value=opener):
            with self.assertRaises(app.Failure) as caught:
                app.transport("POST", "https://oauth.yandex.ru/token")
        self.assertEqual(str(caught.exception), "POST:http_400:invalid_client")


if __name__ == "__main__":
    unittest.main()
