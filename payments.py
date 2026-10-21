"""ระบบชำระเงิน: สร้าง PromptPay QR, ตรวจสลิป, และแลกซอง TrueMoney"""
from __future__ import annotations

import mimetypes
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

import requests
import cloudscraper
import certifi
from dotenv import load_dotenv

load_dotenv()

TH_TZ = timezone(timedelta(hours=7))
TIMEOUT = 30
SLIP_PROVIDER = os.getenv("SLIP_PROVIDER", "slipok")
SLIPOK_API_KEY = os.getenv("SLIPOK_API_KEY", "")
SLIPOK_BRANCH_ID = os.getenv("SLIPOK_BRANCH_ID", "")
EASYSLIP_API_KEY = os.getenv("EASYSLIP_API_KEY", "")
SLIP_MAX_AGE_MINUTES = int(os.getenv("SLIP_MAX_AGE_MINUTES", "60"))
TRUEMONEY_PHONE = os.getenv("PROMPTPAY_PHONE", "") # ใช้เบอร์เดียวกับพร้อมเพย์

EASYSLIP_URL = "https://developer.easyslip.com/api/v1/verify"
SLIPOK_ERRORS = {
    1007: "ไม่พบ QR Code ในสลิป กรุณาส่งรูปสลิปที่เห็น QR ชัดเจน",
    1008: "รูปนี้ไม่ใช่สลิปโอนเงิน",
    1010: "สลิปเพิ่งโอนยังไม่เข้าระบบธนาคาร ลองใหม่ใน 1-2 นาที",
    1011: "ไม่พบสลิปนี้ในระบบธนาคาร",
    1012: "สลิปนี้ถูกใช้ไปแล้ว",
}

class SlipError(Exception):
    def __init__(self, code: str, user_message: str):
        super().__init__(f"{code}: {user_message}")
        self.code = code
        self.user_message = user_message

@dataclass
class SlipResult:
    trans_ref: str
    amount: float
    date: datetime | None
    receiver_name: str = ""
    receiver_accounts: list = field(default_factory=list)

# ---------------------------------------------------------------- PromptPay
def _tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"

def _crc16(data: str) -> str:
    crc = 0xFFFF
    for byte in data.encode("ascii"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"

def _normalize_target(target: str) -> str:
    digits = re.sub(r"\D", "", target)
    if len(digits) == 10 and digits.startswith("0"):
        return "0066" + digits[1:]
    return digits

def promptpay_payload(target: str, amount: float) -> str:
    merchant = _tlv("00", "A000000677010111") + _tlv("01", _normalize_target(target))
    data = (
        _tlv("00", "01")
        + _tlv("01", "12")
        + _tlv("29", merchant)
        + _tlv("53", "764")
        + _tlv("54", f"{amount:.2f}")
        + _tlv("58", "TH")
        + "6304"
    )
    return data + _crc16(data)

# ---------------------------------------------------------------- TrueMoney
def redeem_truemoney(voucher_link: str) -> tuple[bool, float | str, str | None]:
    """
    แลกซอง TrueMoney 
    คืนค่า: (สำเร็จ?, ยอดเงินหรือข้อความ error, voucher_hash)
    """
    if not TRUEMONEY_PHONE:
        return False, "ระบบยังไม่ได้ตั้งค่าเบอร์ TrueMoney ใน .env", None

    u = urlparse(voucher_link.strip().replace(' ', ''))
    if u.scheme != "https" or u.netloc != "gift.truemoney.com" or not u.path.startswith("/campaign"):
        return False, "ลิ้งค์อั่งเปาไม่ถูกต้อง หรือรูปแบบไม่ถูกต้อง", None

    qs = parse_qs(u.query)
    voucher_hash = (qs.get("v") or [None])[0]

    if not voucher_hash or not re.fullmatch(r"[A-Za-z0-9]{18,64}", voucher_hash):
        return False, "ลิ้งค์อั่งเปาไม่ถูกต้อง หรือรูปแบบไม่ถูกต้อง", None

    scraper = cloudscraper.create_scraper()
    try:
        resp = scraper.post(
            url=f'https://gift.truemoney.com/campaign/vouchers/{voucher_hash}/redeem',
            headers={
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/98.0.4758.102 Safari/537.36'
                )
            },
            json={
                'mobile': TRUEMONEY_PHONE,
                'voucher_hash': voucher_hash
            },
            verify=certifi.where(),
            timeout=15
        )
        data = resp.json()
    except Exception as e:
        return False, f"เชื่อมต่อ TrueMoney ไม่ได้: {e}", None

    status_code = data.get('status', {}).get('code', 'UNKNOWN')
    if status_code != 'SUCCESS':
        error_message = data.get('status', {}).get('message', 'เกิดข้อผิดพลาดจาก API')
        return False, error_message, None

    amount_str = data.get('data', {}).get('my_ticket', {}).get('amount_baht')
    if not amount_str:
        return False, "ไม่พบจำนวนเงินในซอง", None

    return True, float(amount_str), voucher_hash

# ---------------------------------------------------------------- Slip API
def _get(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d

def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None

def _mask_match(masked: str, full: str) -> bool:
    m = re.sub(r"[^0-9x*•]", "", masked.lower())
    f = re.sub(r"\D", "", full or "")
    if not m or not f:
        return False
    n = min(len(m), len(f))
    visible = 0
    for a, b in zip(m[-n:], f[-n:]):
        if a.isdigit():
            if a != b:
                return False
            visible += 1
    return visible >= 3

def _verify_slipok(image: bytes, filename: str) -> SlipResult:
    if not SLIPOK_API_KEY or not SLIPOK_BRANCH_ID:
        raise SlipError("config", "ระบบตรวจสลิปยังไม่ได้ตั้งค่า")
    mime = mimetypes.guess_type(filename)[0] or "image/jpeg"
    r = requests.post(
        f"https://api.slipok.com/api/line/apikey/{SLIPOK_BRANCH_ID}",
        headers={"x-authorization": SLIPOK_API_KEY},
        files={"files": (filename, image, mime)},
        data={"log": "true"},
        timeout=TIMEOUT,
    )
    body = r.json()
    if not body.get("success") or not body.get("data"):
        code = body.get("code")
        raise SlipError(str(code), SLIPOK_ERRORS.get(code, "ตรวจสลิปไม่สำเร็จ"))

    d = body["data"]
    try:
        date = datetime.strptime(
            f"{d['transDate']} {d['transTime']}", "%Y%m%d %H:%M:%S"
        ).replace(tzinfo=TH_TZ)
    except (KeyError, ValueError):
        date = _parse_iso(d.get("transTimestamp"))

    receiver = d.get("receiver") or {}
    accounts = [
        str(v)
        for v in (_get(receiver, "proxy", "value"), _get(receiver, "account", "value"))
        if v
    ]
    return SlipResult(
        trans_ref=str(d.get("transRef") or ""),
        amount=float(d.get("amount") or 0),
        date=date,
        receiver_name=receiver.get("displayName") or "",
        receiver_accounts=accounts,
    )

def _verify_easyslip(image: bytes, filename: str) -> SlipResult:
    if not EASYSLIP_API_KEY:
        raise SlipError("config", "ระบบตรวจสลิปยังไม่ได้ตั้งค่า")
    mime = mimetypes.guess_type(filename)[0] or "image/jpeg"
    r = requests.post(
        EASYSLIP_URL,
        headers={"Authorization": f"Bearer {EASYSLIP_API_KEY}"},
        files={"file": (filename, image, mime)},
        timeout=TIMEOUT,
    )
    body = r.json()
    if body.get("status") != 200 or not body.get("data"):
        msg = str(body.get("message", "unknown"))
        raise SlipError(msg, f"ตรวจสลิปไม่สำเร็จ ({msg})")

    d = body["data"]
    amt = d.get("amount")
    raw = amt.get("amount") if isinstance(amt, dict) else amt
    names = _get(d, "receiver", "account", "name") or {}
    accounts = [
        str(v)
        for v in (
            _get(d, "receiver", "account", "proxy", "account"),
            _get(d, "receiver", "account", "bank", "account"),
        )
        if v
    ]
    return SlipResult(
        trans_ref=str(d.get("transRef") or ""),
        amount=float(raw or 0),
        date=_parse_iso(d.get("date")),
        receiver_name=names.get("th") or names.get("en") or "",
        receiver_accounts=accounts,
    )

def _validate(slip: SlipResult, expected_amount: float, receiver_phone: str) -> None:
    if not any(_mask_match(acc, receiver_phone) for acc in slip.receiver_accounts):
        raise SlipError("receiver", "สลิปนี้ไม่ได้โอนเข้าบัญชีของร้าน")
    if abs(slip.amount - expected_amount) >= 0.005:
        raise SlipError(
            "amount",
            f"ยอดในสลิป {slip.amount:,.2f} บาท ไม่ตรงกับยอดที่ต้องโอน {expected_amount:,.2f} บาท",
        )
    if slip.date is None:
        raise SlipError("no_date", "อ่านวันเวลาในสลิปไม่ได้")
    if slip.date.tzinfo is None:
        slip.date = slip.date.replace(tzinfo=TH_TZ)
    age = datetime.now(timezone.utc) - slip.date
    if age > timedelta(minutes=SLIP_MAX_AGE_MINUTES):
        raise SlipError("too_old", f"สลิปเก่าเกินไป (รับเฉพาะที่โอนภายใน {SLIP_MAX_AGE_MINUTES} นาที)")
    if age < timedelta(minutes=-5):
        raise SlipError("future", "วันเวลาในสลิปไม่ถูกต้อง")

def verify_slip(image: bytes, filename: str, expected_amount: float, receiver_phone: str) -> SlipResult:
    try:
        if SLIP_PROVIDER == "slipok":
            slip = _verify_slipok(image, filename)
        else:
            slip = _verify_easyslip(image, filename)
    except requests.RequestException:
        raise SlipError("network", "เชื่อมต่อระบบตรวจสลิปไม่ได้ ลองใหม่ภายหลัง")
    except ValueError:
        raise SlipError("api", "ระบบตรวจสลิปตอบกลับผิดรูปแบบ ลองใหม่ภายหลัง")

    if not slip.trans_ref:
        raise SlipError("no_ref", "อ่านเลขอ้างอิงสลิปไม่ได้")
    _validate(slip, expected_amount, receiver_phone)
    return slip
