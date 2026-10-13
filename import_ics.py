#!/usr/bin/env python3
"""ICS → личные копии событий в CalDAV Яндекса. Без --apply записи нет."""

import argparse
import copy
import csv
import hashlib
import json
import math
import os
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from icalendar import Calendar

D = "{DAV:}"
C = "{urn:ietf:params:xml:ns:caldav}"
BASE = "https://caldav.yandex.ru/"
LIMIT = 64 * 1024 * 1024
OAUTH_CODES = {"invalid_request", "invalid_client", "invalid_grant",
               "unauthorized_client", "invalid_scope", "access_denied"}


class Failure(Exception):
    """Сообщения не содержат HTTP-тела, секретов или содержимого событий."""


class RateLimited(Failure):
    def __init__(self, method, retry_after=None):
        super().__init__(f"{method}:http_429")
        self.retry_after = retry_after


class TemporaryServerError(Failure):
    def __init__(self, method, status, retry_after=None):
        super().__init__(f"{method}:http_{status}")
        self.status = status
        self.retry_after = retry_after


class RequestPacer:
    def __init__(self, interval=2.0):
        self.interval = interval
        self.next_start = 0.0

    def wait(self):
        delay = self.next_start - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_start = time.monotonic() + self.interval


PACER = RequestPacer()
MAX_RATE_RETRIES = 3


def retry_after_seconds(value):
    if not value:
        return None
    try:
        if value.strip().isdigit():
            return int(value.strip())
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0, (target - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def wait_rate_limit(error, attempt):
    delay = error.retry_after if error.retry_after is not None else 30 * 2 ** attempt
    print(f"HTTP 429: ожидание {delay:.1f} с, повтор {attempt + 1}/{MAX_RATE_RETRIES}", flush=True)
    time.sleep(delay)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Не передаём Authorization на другой адрес.


def config():
    names = ("CLIENT_ID", "CLIENT_SECRET", "USER_EMAIL")
    values = {}
    path = Path(__file__).resolve().with_name(".env")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        lines = []
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise Failure(f"env_line_{number}: ожидается KEY=value")
        key, value = key.strip(), value.strip()
        if key not in names:
            continue
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise Failure(f"env_line_{number}: незакрытые кавычки")
            value = value[1:-1]
        values[key] = value
    return {name: os.environ.get(name, values.get(name, "")).strip() for name in names}


def transport(method, url, *, token=None, data=None, headers=None):
    for attempt in range(MAX_RATE_RETRIES + 1):
        try:
            return transport_once(method, url, token=token, data=data, headers=headers)
        except RateLimited as exc:
            # PUT повторяется отдельно, только после поиска UID.
            if method not in {"GET", "PROPFIND", "REPORT", "POST"} or attempt == MAX_RATE_RETRIES:
                raise
            wait_rate_limit(exc, attempt)
        except TemporaryServerError as exc:
            if method not in {"GET", "REPORT", "PROPFIND"} or attempt == MAX_RATE_RETRIES:
                raise
            delay = max(5 * 2 ** attempt, exc.retry_after or 0)
            print(f"{method}: HTTP {exc.status}, ожидание {delay:.1f} с, "
                  f"повтор {attempt + 1}/{MAX_RATE_RETRIES}", flush=True)
            time.sleep(delay)


def transport_once(method, url, *, token=None, data=None, headers=None):
    headers = dict(headers or {})
    if token:
        if any(not 33 <= ord(ch) <= 126 for ch in token):
            raise Failure("invalid_token_characters")
        headers["Authorization"] = f"OAuth {token}"
    req = Request(url, data=data, headers=headers, method=method)
    PACER.wait()
    try:
        with build_opener(NoRedirect()).open(req, timeout=60) as response:
            body = response.read(LIMIT + 1)
            if len(body) > LIMIT:
                raise Failure("response_too_large")
            return response.status, body
    except HTTPError as error:
        detail = ""
        retry_after = retry_after_seconds(error.headers.get("Retry-After"))
        try:
            if url == "https://oauth.yandex.ru/token":
                try:
                    parsed = json.loads(error.read(16384))
                    code = parsed.get("error") if isinstance(parsed, dict) else None
                    if isinstance(code, str) and code in OAUTH_CODES:
                        detail = ":" + code
                except (ValueError, OSError):
                    pass
        finally:
            error.close()
        if error.code == 429:
            raise RateLimited(method, retry_after) from None
        if error.code in {502, 503, 504}:
            raise TemporaryServerError(method, error.code, retry_after) from None
        raise Failure(f"{method}:http_{error.code}{detail}") from None
    except (URLError, OSError):
        raise Failure(f"{method}:network_error") from None


def user_token(settings):
    for key in ("CLIENT_ID", "CLIENT_SECRET", "USER_EMAIL"):
        value = settings[key]
        if not value or any(not 33 <= ord(ch) <= 126 for ch in value):
            raise Failure(f"{key}: требуется значение ASCII без пробелов")
    _, body = transport("POST", "https://oauth.yandex.ru/token", data=urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": settings["CLIENT_ID"], "client_secret": settings["CLIENT_SECRET"],
        "subject_token": settings["USER_EMAIL"],
        "subject_token_type": "urn:yandex:params:oauth:token-type:email",
    }).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        token = json.loads(body)["access_token"]
        if not isinstance(token, str) or not token:
            raise ValueError()
        return token
    except (ValueError, KeyError, TypeError):
        raise Failure("oauth:invalid_response") from None


def safe_url(href, base=BASE):
    url = urljoin(base, href)
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != "caldav.yandex.ru"
            or parts.query or parts.fragment):
        raise Failure("unexpected_caldav_url")
    return url


def xml(body):
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise Failure("unsafe_xml")
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        raise Failure("invalid_xml") from None


def properties(root):
    if root.tag != D + "multistatus":
        raise Failure("expected_multistatus")
    for response in root.findall(D + "response"):
        href = response.findtext(D + "href")
        if not href:
            raise Failure("missing_resource_href")
        for propstat in response.findall(D + "propstat"):
            status = propstat.findtext(D + "status", "").split()
            if len(status) >= 2 and status[1] == "200":
                prop = propstat.find(D + "prop")
                if prop is not None:
                    yield href, prop


class CalDAV:
    def __init__(self, settings):
        self.settings = settings
        self.token = None
        self.expires = 0

    def call(self, method, url, data=None, headers=None):
        if time.monotonic() >= self.expires:
            self.token = user_token(self.settings)
            self.expires = time.monotonic() + 3000
        return transport(method, safe_url(url), token=self.token, data=data, headers=headers)

    def propfind(self, url, tags, depth="0"):
        root = ET.Element(D + "propfind")
        prop = ET.SubElement(root, D + "prop")
        for tag in tags:
            ET.SubElement(prop, tag)
        _, body = self.call("PROPFIND", url, ET.tostring(root), {
            "Depth": depth, "Content-Type": "application/xml; charset=utf-8",
        })
        return list(properties(xml(body)))

    def calendars(self):
        principal = None
        for _, prop in self.propfind(BASE, [D + "current-user-principal"]):
            principal = prop.findtext(D + "current-user-principal/" + D + "href") or principal
        if not principal:
            raise Failure("principal_not_found")
        home = None
        for _, prop in self.propfind(safe_url(principal), [C + "calendar-home-set"]):
            home = prop.findtext(C + "calendar-home-set/" + D + "href") or home
        if not home:
            raise Failure("calendar_home_not_found")
        found = {}
        for href, prop in self.propfind(safe_url(home), [D + "displayname", D + "resourcetype"], "1"):
            if prop.find(D + "resourcetype/" + C + "calendar") is not None:
                found[safe_url(href)] = prop.findtext(D + "displayname", "")
        return found

    def find_event(self, url, uid):
        """Ищет реальный ресурс по UID, не предполагая имя файла на сервере."""
        root = ET.Element(C + "calendar-query")
        prop = ET.SubElement(root, D + "prop")
        ET.SubElement(prop, C + "calendar-data")
        filt = ET.SubElement(root, C + "filter")
        cal = ET.SubElement(filt, C + "comp-filter", name="VCALENDAR")
        event = ET.SubElement(cal, C + "comp-filter", name="VEVENT")
        uid_filter = ET.SubElement(event, C + "prop-filter", name="UID")
        ET.SubElement(uid_filter, C + "text-match", collation="i;octet").text = uid
        _, body = self.call("REPORT", url, ET.tostring(root), {
            "Depth": "1", "Content-Type": "application/xml; charset=utf-8",
        })
        document = xml(body)
        matches, complete = [], set()
        for href, prop in properties(document):
            value = prop.findtext(C + "calendar-data")
            if value is None:
                continue
            complete.add(href)
            parsed = Calendar.from_ical(value)
            events = parsed.walk("VEVENT")
            if events and all(str(e.get("UID", "")) == uid for e in events):
                matches.append(value.encode("utf-8"))
        if len(complete) != len(document.findall(D + "response")):
            raise Failure("incomplete_verification_report")
        if len(matches) > 1:
            raise Failure("multiple_uid_resources")
        return matches[0] if matches else None

    def existing_uids(self, url):
        root = ET.Element(C + "calendar-query")
        prop = ET.SubElement(root, D + "prop")
        data = ET.SubElement(prop, C + "calendar-data")
        cal = ET.SubElement(data, C + "comp", name="VCALENDAR")
        event = ET.SubElement(cal, C + "comp", name="VEVENT")
        ET.SubElement(event, C + "prop", name="UID")
        filt = ET.SubElement(root, C + "filter")
        cal = ET.SubElement(filt, C + "comp-filter", name="VCALENDAR")
        ET.SubElement(cal, C + "comp-filter", name="VEVENT")
        _, body = self.call("REPORT", url, ET.tostring(root), {
            "Depth": "1", "Content-Type": "application/xml; charset=utf-8",
        })
        document = xml(body)
        # Не считаем частичный REPORT полным списком UID.
        responses = document.findall(D + "response")
        seen_resources, uids = set(), set()
        for href, prop in properties(document):
            value = prop.findtext(C + "calendar-data")
            if value is None:
                continue
            parsed = Calendar.from_ical(value)
            events = parsed.walk("VEVENT")
            if not events or any(not e.get("UID") for e in events):
                raise Failure("incomplete_uid_report")
            uids.update(str(e["UID"]) for e in events)
            seen_resources.add(href)
        if len(seen_resources) != len(responses):
            raise Failure("incomplete_uid_report")
        return uids


def prepare(folder, sources=None):
    """Собирает серию и исключения из всех файлов до первой записи в API."""
    if not folder.is_dir():
        raise Failure("folder_not_found")
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".ics")
    if not files:
        raise Failure("no_ics_files")
    groups, zones = defaultdict(dict), {}
    components = 0
    for index, path in enumerate(files, 1):
        try:
            if path.stat().st_size > LIMIT:
                raise ValueError()
            source = Calendar.from_ical(path.read_bytes().decode("utf-8-sig"))
            if source.name != "VCALENDAR" or any(c.errors for c in source.walk()):
                raise ValueError()
            if any(c.name not in {"VEVENT", "VTIMEZONE"} for c in source.subcomponents):
                raise Failure(f"file_{index}: unsupported_component")
            for zone in source.walk("VTIMEZONE"):
                key = str(zone["TZID"])
                if key in zones and zones[key].to_ical() != zone.to_ical():
                    raise Failure(f"file_{index}: conflicting_timezone")
                zones[key] = zone
            for original in source.walk("VEVENT"):
                event = copy.deepcopy(original)
                uid = str(event["UID"])
                if sources is not None:
                    sources.setdefault(uid, set()).add(path.name)
                if not uid or "DTSTART" not in event or "DTSTAMP" not in event:
                    raise ValueError()
                # Копии для просмотра; не отправляем приглашения и email-напоминания.
                for key in ("ATTENDEE", "ORGANIZER", "X-TELEMOST-REQUIRED"):
                    event.pop(key, None)
                event.subcomponents = []  # VALARM не переносим.
                rid = event.get("RECURRENCE-ID")
                key = rid.to_ical() if rid is not None else b"master"
                previous = groups[uid].get(key)
                if previous is not None and previous.to_ical() != event.to_ical():
                    raise Failure(f"file_{index}: conflicting_uid_recurrence")
                groups[uid][key] = event
                components += 1
        except Failure:
            raise
        except Exception:
            raise Failure(f"file_{index}: invalid_ics (проверьте UID, DTSTART, DTSTAMP и формат)") from None
    output = {}
    for uid, events in groups.items():
        if b"master" not in events:
            raise Failure("orphan_recurrence: исключение без основного события")
        cal = Calendar()
        cal.add("PRODID", "-//Local ICS Import//RU")
        cal.add("VERSION", "2.0")
        cal.add("CALSCALE", "GREGORIAN")
        for zone in zones.values():
            cal.add_component(copy.deepcopy(zone))
        for event in events.values():
            cal.add_component(event)
        output[uid] = cal.to_ical()
    if not output:
        raise Failure("no_events")
    return output, len(files), components


def read_created_event(client, calendar_url, resource, uid):
    try:
        _, body = client.call("GET", resource)
        return body
    except Failure as exc:
        if str(exc) != "GET:http_404":
            raise
    # Сервер может использовать другой href или показывать запись с задержкой.
    # Повторяем только чтение, никогда PUT.
    for delay in (0, 1, 2):
        if delay:
            time.sleep(delay)
        body = client.find_event(calendar_url, uid)
        if body is not None:
            return body
    raise Failure("verification_uid_not_found")


def create_and_read(client, calendar_url, resource, uid, payload):
    for attempt in range(MAX_RATE_RETRIES + 1):
        try:
            code, _ = client.call("PUT", resource, payload, {
                "Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*",
            })
        except RateLimited as exc:
            if attempt == MAX_RATE_RETRIES:
                raise
            wait_rate_limit(exc, attempt)
            existing = client.find_event(calendar_url, uid)
            if existing is not None:
                return existing
            continue
        if code != 201:
            raise Failure("PUT:unexpected_status")
        return read_created_event(client, calendar_url, resource, uid)


REPORT_FIELDS = ["uid_sha256", "status", "error", "uid", "summary", "start", "end",
                 "start_timezone", "end_timezone", "all_day", "duration", "rrule",
                 "exception_count", "source_files"]


def report_details(uid, payload, sources=None):
    events = Calendar.from_ical(payload).walk("VEVENT")
    master = next(e for e in events if "RECURRENCE-ID" not in e)
    def date_field(key):
        prop = master.get(key)
        if prop is None:
            return "", ""
        value = prop.dt
        zone = str(prop.params.get("TZID") or getattr(value, "tzinfo", None) or
                   ("floating" if hasattr(value, "hour") else "date"))
        return value.isoformat(), zone
    start, start_zone = date_field("DTSTART")
    end, end_zone = date_field("DTEND")
    return {
        "uid": uid, "summary": str(master.get("SUMMARY", "")),
        "start": start, "end": end, "start_timezone": start_zone, "end_timezone": end_zone,
        "all_day": not hasattr(master["DTSTART"].dt, "hour"),
        "duration": master["DURATION"].to_ical().decode() if "DURATION" in master else "",
        "rrule": master["RRULE"].to_ical().decode() if "RRULE" in master else "",
        "exception_count": sum("RECURRENCE-ID" in e for e in events),
        "source_files": json.dumps(sorted((sources or {}).get(uid, [])), ensure_ascii=False),
    }


def csv_safe(row):
    # Текст ICS не должен становиться формулой при открытии CSV в Excel.
    return {key: ("'" + value if isinstance(value, str) and
                  value.lstrip().startswith(("=", "+", "-", "@")) else value)
            for key, value in row.items()}


def import_events(client, calendar_url, events, report, sources=None):
    known = client.existing_uids(calendar_url)
    counts = defaultdict(int)
    with report.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REPORT_FIELDS, delimiter=";")
        writer.writeheader()
        for uid, payload in events.items():
            digest = hashlib.sha256(uid.encode()).hexdigest()
            status, error = "skipped", ""
            if uid not in known:
                resource = calendar_url.rstrip("/") + "/" + digest + ".ics"
                try:
                    body = create_and_read(client, calendar_url, resource, uid, payload)
                    saved = Calendar.from_ical(body)
                    expected = Calendar.from_ical(payload)
                    def identities(cal):
                        return {(str(e.get("UID", "")),
                                 e["RECURRENCE-ID"].to_ical() if "RECURRENCE-ID" in e else b"master")
                                for e in cal.walk("VEVENT")}
                    if identities(saved) != identities(expected):
                        raise Failure("verification_failed")
                    status = "created"
                    known.add(uid)
                except Exception as exc:
                    # При сетевом сбое PUT мог примениться. Повторно здесь не отправляем.
                    status = "needs_check"
                    error = str(exc) if isinstance(exc, Failure) else type(exc).__name__
            counts[status] += 1
            row = report_details(uid, payload, sources)
            row.update(uid_sha256=digest, status=status, error=error)
            writer.writerow(csv_safe(row))
            stream.flush()
            if status == "needs_check":
                # Останавливаем запись при неопределённом исходе или отказе API.
                break
    print(" ".join(f"{key}={value}" for key, value in counts.items()))
    print(f"report={report.resolve()}")
    return 1 if counts["needs_check"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, help="Папка ICS, без обхода подпапок")
    parser.add_argument("--list-calendars", action="store_true")
    parser.add_argument("--calendar-url", help="Точный URL из --list-calendars")
    parser.add_argument("--apply", action="store_true", help="Разрешить запись новых UID")
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="Повторять импорт, минимум 60 секунд")
    parser.add_argument("--request-interval", type=float, default=2.0,
                        help="Минимальный интервал между HTTP-запросами, секунды (по умолчанию 2)")
    args = parser.parse_args()
    if not math.isfinite(args.request_interval) or args.request_interval < 0:
        parser.error("--request-interval должен быть конечным числом не меньше нуля")
    PACER.interval = args.request_interval
    if args.watch is not None and (args.watch < 60 or not args.apply):
        parser.error("--watch требует --apply и интервал не меньше 60 секунд")
    if args.list_calendars and (args.apply or args.folder or args.watch):
        parser.error("--list-calendars запускается отдельно")
    if not args.list_calendars and not args.folder:
        parser.error("Укажите --folder или --list-calendars")
    if args.apply and not args.calendar_url:
        parser.error("Для --apply требуется --calendar-url")
    try:
        if args.list_calendars:
            for url, name in CalDAV(config()).calendars().items():
                print(f"{name}\n  {url}")
            return 0
        client = None
        while True:
            sources = {}
            events, files, components = prepare(args.folder, sources)
            print(f"files={files} VEVENT={components} unique_UID={len(events)}")
            if not args.apply:
                print("Локальная проверка завершена. Запросы к API и запись не выполнялись.")
                return 0
            if client is None:
                client = CalDAV(config())
                if safe_url(args.calendar_url) not in client.calendars():
                    raise Failure("calendar_not_found: используйте точный URL из --list-calendars")
            report = Path(f"import_report_{datetime.now(timezone.utc):%Y%m%d_%H%M%S_%f}.csv")
            result = import_events(client, args.calendar_url, events, report, sources)
            if result or not args.watch:
                return result
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("Остановлено. Последнюю запись при необходимости проверьте в календаре.", file=sys.stderr)
        return 130
    except Exception as exc:
        detail = str(exc) if isinstance(exc, Failure) else type(exc).__name__
        print(f"Ошибка: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
