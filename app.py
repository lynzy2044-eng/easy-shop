import base64
import io
import os
import random
import secrets
import re
from datetime import datetime, timedelta, timezone
from functools import wraps

import qrcode
from dotenv import load_dotenv
from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from notify import discord_notify
from payments import (SlipError, promptpay_payload, verify_slip, redeem_truemoney)

load_dotenv()

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
    SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///shop.db"),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024, # 10MB สำหรับอัปโหลดแบนเนอร์
    GOOGLE_CLIENT_ID=os.getenv("GOOGLE_CLIENT_ID", ""),
    GOOGLE_CLIENT_SECRET=os.getenv("GOOGLE_CLIENT_SECRET", ""),
    PROMPTPAY_PHONE=os.getenv("PROMPTPAY_PHONE", ""),
    ADMIN_EMAIL=os.getenv("ADMIN_EMAIL", "")
)

# แก้ไข URL ของ PostgreSQL ให้ Flask-SQLAlchemy รู้จัก (บางที Vercel ให้มาเป็น postgres://)
if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgres://"):
    app.config["SQLALCHEMY_DATABASE_URI"] = app.config["SQLALCHEMY_DATABASE_URI"].replace("postgres://", "postgresql://", 1)

db = SQLAlchemy(app)

SHOP_NAME = os.getenv("SHOP_NAME", "CapCut PC Course")
PROMPTPAY_PHONE = os.getenv("PROMPTPAY_PHONE", "")
ORDER_TTL = timedelta(minutes=30)

# ดึงอีเมลแอดมินและแปลงเป็นตัวพิมพ์เล็กเพื่อความแม่นยำ
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()

# ราคาเป็นตัวอย่าง แก้ได้ตามต้องการ
PACKAGES = {
    "basic": {
        "name": "Basic", "price": 10, "tracks": ["basic"],
        "description": "ตัดต่อพื้นฐาน ตัดคลิปจนส่งออกได้เองทุกขั้นตอน",
    },
    "advanced": {
        "name": "Advanced", "price": 890, "tracks": ["advanced"],
        "description": "จังหวะ คีย์เฟรม สี และเสียง ทำให้งานดูโปร",
    },
    "bundle": {
        "name": "Bundle", "price": 1090, "tracks": ["basic", "advanced"],
        "description": "ครบทั้ง Basic และ Advanced ประหยัดกว่าซื้อแยก",
    },
}


# ---------------------------------------------------------------- Models
def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False) # เปลี่ยนเป็น Username
    password_hash = db.Column(db.String(255), nullable=False)
    balance = db.Column(db.Float, default=0.0)  # กระเป๋าเงิน
    created_at = db.Column(db.DateTime, default=utcnow)


class Lesson(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    track = db.Column(db.String(20), nullable=False)  # basic / advanced
    position = db.Column(db.Integer, default=0)
    title = db.Column(db.String(150), nullable=False)
    video_url = db.Column(db.String(300), nullable=False)  # ลิงก์ embed (YouTube unlisted)
    is_free = db.Column(db.Boolean, default=False)


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    package = db.Column(db.String(20), nullable=False)
    amount_satang = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(10), default="pending")  # pending / paid / expired
    created_at = db.Column(db.DateTime, default=utcnow)
    paid_at = db.Column(db.DateTime)
    slip_ref = db.Column(db.String(100), unique=True)


class Enrollment(db.Model):
    """บันทึกว่า user คนไหนเคยเข้าเรียนบทไหน (ใช้กับบทเรียนฟรี)"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lesson.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    __table_args__ = (db.UniqueConstraint("user_id", "lesson_id", name="uq_enroll"),)

    lesson = db.relationship("Lesson", backref="enrollments")


class TopupHistory(db.Model):
    """ประวัติการเติมเงินผ่านช่องทางต่างๆ"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    method = db.Column(db.String(20), nullable=False)  # truemoney / promptpay
    amount = db.Column(db.Float, nullable=False)
    ref = db.Column(db.String(100), unique=True)  # voucher_hash หรือ trans_ref
    created_at = db.Column(db.DateTime, default=utcnow)


class SiteSetting(db.Model):
    """ตารางสำหรับเก็บข้อความและรูปภาพหน้าเว็บ (CMS)"""
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Text, nullable=False)


# ---------------------------------------------------------------- Helpers
def get_setting(key, default=""):
    setting = db.session.get(SiteSetting, key)
    return setting.value if setting else default


def set_setting(key, value):
    setting = db.session.get(SiteSetting, key)
    if setting:
        setting.value = value
    else:
        db.session.add(SiteSetting(key=key, value=value))
    db.session.commit()


def current_user():
    uid = session.get("uid")
    return db.session.get(User, uid) if uid else None


def owned_tracks(user):
    if not user:
        return set()
    tracks = set()
    for o in Order.query.filter_by(user_id=user.id, status="paid"):
        tracks.update(PACKAGES[o.package]["tracks"])
    return tracks


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            flash("กรุณาเข้าสู่ระบบก่อนเข้าใช้งาน", "err")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            flash("กรุณาเข้าสู่ระบบก่อน", "err")
            return redirect(url_for("login"))
        
        # ตรวจสอบสิทธิ์แบบไม่สนตัวพิมพ์เล็ก-ใหญ่
        if user.username.strip().lower() != ADMIN_EMAIL:
            flash(f"❌ คุณไม่มีสิทธิ์เข้าถึงหน้านี้ (Username ที่ใช้คือ: {user.username})", "err")
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapper


def new_order_amount(price_baht):
    """สุ่มสตางค์ 0.01-0.99 ที่ไม่ซ้ำกับออเดอร์ที่กำลังรอจ่าย"""
    cutoff = utcnow() - ORDER_TTL
    active = Order.query.filter(Order.status == "pending", Order.created_at > cutoff).all()
    used = {o.amount_satang for o in active}
    base = price_baht * 100
    options = [base + c for c in range(1, 100) if base + c not in used]
    if not options:
        abort(503)
    return random.choice(options)


def qr_data_uri(text):
    buf = io.BytesIO()
    qrcode.make(text).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@app.context_processor
def inject_globals():
    user = current_user()
    # ตรวจสอบสิทธิ์แอดมินเพื่อส่งไปให้หน้าเว็บ (ใช้โชว์ปุ่ม)
    is_admin = False
    if user and ADMIN_EMAIL:
        is_admin = user.username.strip().lower() == ADMIN_EMAIL

    return {
        "user": user,
        "shop_name": SHOP_NAME,
        "is_admin": is_admin,
        # ส่งค่าจาก CMS ไปให้หน้าเว็บ
        "hero_title": get_setting("hero_title", "เรียนตัดต่อ CapCut PC ตั้งแต่ศูนย์"),
        "hero_subtitle": get_setting("hero_subtitle", "จนทำงานได้สไตล์ของตัวเอง ทดลองเรียนฟรีได้ทุกบทแรก"),
        "hero_img": get_setting("hero_img", ""),
        "btn_text": get_setting("btn_text", "ดูแพ็กเกจเลย"),
        "btn_size": get_setting("btn_size", "14px 32px")
    }


@app.errorhandler(413)
def file_too_large(_e):
    flash("ไฟล์รูปใหญ่เกิน 10 MB กรุณาลดขนาดรูปภาพ", "err")
    return redirect(request.referrer or url_for("index"))


# ---------------------------------------------------------------- Admin Routes
@app.route("/admin")
@admin_required
def admin_dashboard():
    total_topup = db.session.query(db.func.sum(TopupHistory.amount)).scalar() or 0
    tm_total = db.session.query(db.func.sum(TopupHistory.amount)).filter_by(method="truemoney").scalar() or 0
    pp_total = db.session.query(db.func.sum(TopupHistory.amount)).filter_by(method="promptpay").scalar() or 0

    histories = TopupHistory.query.order_by(TopupHistory.created_at.desc()).limit(50).all()
    users = User.query.order_by(User.created_at.desc()).all()

    return render_template("admin.html", total=total_topup, tm=tm_total, pp=pp_total, histories=histories, users=users)


@app.route("/admin/settings", methods=["POST"])
@admin_required
def admin_settings():
    set_setting("hero_title", request.form.get("hero_title", ""))
    set_setting("hero_subtitle", request.form.get("hero_subtitle", ""))
    set_setting("btn_text", request.form.get("btn_text", ""))
    set_setting("btn_size", request.form.get("btn_size", "14px 32px"))

    # อัปโหลดรูปแบนเนอร์
    file = request.files.get("hero_img")
    if file and file.filename:
        # แปลงรูปเป็น Base64 เก็บใน DB (วิธีง่ายสุดสำหรับ Vercel)
        img_bytes = file.read()
        b64 = base64.b64encode(img_bytes).decode('utf-8')
        ext = file.filename.split('.')[-1]
        set_setting("hero_img", f"data:image/{ext};base64,{b64}")

    flash("✅ อัปเดตหน้าเว็บเรียบร้อยแล้ว", "ok")
    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------------- Pages
@app.route("/")
def index():
    user = current_user()
    lessons = Lesson.query.order_by(Lesson.track, Lesson.position).all()
    return render_template("index.html", packages=PACKAGES, lessons=lessons, owned=owned_tracks(user))


@app.route("/free")
def free_lessons():
    """หน้าเรียนฟรีแยกจากแพ็กเกจเสียเงิน"""
    lessons = (
        Lesson.query
        .filter_by(is_free=True)
        .order_by(Lesson.track, Lesson.position)
        .all()
    )
    return render_template("free.html", lessons=lessons)


@app.route("/lesson/<int:lesson_id>")
def lesson(lesson_id):
    user = current_user()
    if not user:
        flash("กรุณาเข้าสู่ระบบก่อนเข้าเรียน", "err")
        return redirect(url_for("login", next=request.path))

    item = db.get_or_404(Lesson, lesson_id)

    # บทเรียนฟรี → บันทึก enrollment ครั้งแรกที่เข้า
    if item.is_free:
        exists = Enrollment.query.filter_by(
            user_id=user.id, lesson_id=item.id
        ).first()
        if not exists:
            db.session.add(Enrollment(user_id=user.id, lesson_id=item.id))
            db.session.commit()
        return render_template("lesson.html", lesson=item)

    # บทเรียนเสียเงิน → ต้องมีสิทธิ์ใน track นั้น
    if item.track not in owned_tracks(user):
        flash("บทเรียนนี้สำหรับสมาชิกแพ็กเกจเท่านั้น", "err")
        return redirect(url_for("index") + "#packages")

    return render_template("lesson.html", lesson=item)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        # ตรวจสอบความปลอดภัยของรหัสผ่านขั้นต่ำ
        if not re.search(r"[a-zA-Z]", password) or not re.search(r"[0-9]", password):
            flash("❌ รหัสผ่านต้องมีทั้งตัวอักษรและตัวเลข", "err")
        elif len(password) < 6:
            flash("❌ รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร", "err")
        elif password != confirm_password:
            flash("❌ รหัสผ่านทั้งสองช่องไม่ตรงกัน", "err")
        elif User.query.filter_by(username=username).first():
            flash("❌ Username นี้ถูกใช้งานแล้ว กรุณาใช้ชื่ออื่น", "err")
        else:
            user = User(username=username, password_hash=generate_password_hash(password))
            db.session.add(user)
            db.session.commit()
            session.clear()
            session["uid"] = user.id
            flash("สมัครสมาชิกสำเร็จ ยินดีต้อนรับ!", "ok")
            return redirect(url_for("index"))
            
    return render_template("auth.html", mode="register")

@app.route("/check_username", methods=["POST"])
def check_username():
    data = request.get_json()
    username = data.get("username", "").strip().lower()
    if not username:
        return jsonify(available=False, message="กรุณากรอก Username")
        
    user = User.query.filter_by(username=username).first()
    if user:
        return jsonify(available=False, message="❌ Username นี้มีคนใช้แล้ว")
    return jsonify(available=True, message="✅ Username นี้ใช้งานได้")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password_hash, password):
            session.clear()
            session["uid"] = user.id
            flash("เข้าสู่ระบบสำเร็จ", "ok")
            return redirect(url_for("index"))

        flash("Username หรือรหัสผ่านไม่ถูกต้อง", "err")
    return render_template("auth.html", mode="login")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        new_password = request.form.get("new_password", "")

        user = User.query.filter_by(username=username).first()
        if not user:
            flash("❌ ไม่พบ Username นี้ในระบบ", "err")
        elif len(new_password) < 6:
            flash("❌ รหัสผ่านใหม่ต้องมีอย่างน้อย 6 ตัวอักษร", "err")
        else:
            user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash("✅ เปลี่ยนรหัสผ่านสำเร็จ! กรุณาเข้าสู่ระบบ", "ok")
            return redirect(url_for("login"))
            
    return render_template("forgot.html")


@app.route("/auth/google", methods=["POST"])
def auth_google():
    """รับ Code จาก Google OAuth2 แล้วแลกเป็นข้อมูล User"""
    if not app.config["GOOGLE_CLIENT_ID"] or not app.config["GOOGLE_CLIENT_SECRET"]:
        return jsonify(ok=False, error="google_not_configured"), 500

    data = request.get_json(silent=True) or {}
    code = data.get("code")
    if not code:
        return jsonify(ok=False, error="no_code"), 400

    try:
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": app.config["GOOGLE_CLIENT_ID"],
                    "client_secret": app.config["GOOGLE_CLIENT_SECRET"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=["openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"],
        )
        flow.redirect_uri = "postmessage"
        flow.fetch_token(code=code)

        credentials = flow.credentials
        id_info = id_token.verify_oauth2_token(
            credentials.id_token,
            google_requests.Request(),
            app.config["GOOGLE_CLIENT_ID"]
        )

        email = id_info.get("email", "").strip().lower()
        if not email:
            raise ValueError("No email found in token")

        email_verified = id_info.get("email_verified", False)
        if not email_verified:
            return jsonify(ok=False, error="email_not_verified"), 401

        user = User.query.filter_by(username=email).first()
        if not user:
            user = User(
                username=email,
                password_hash=generate_password_hash(secrets.token_hex(16)),
            )
            db.session.add(user)
            db.session.commit()

        session.clear()
        session["uid"] = user.id
        return jsonify(ok=True, redirect=url_for("index"))

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify(ok=False, error=str(e)), 401


@app.route("/logout")
def logout():
    session.clear()
    flash("ออกจากระบบแล้ว", "ok")
    return redirect(url_for("index"))


# ---------------------------------------------------------------- Topup Routes
@app.route("/topup", methods=["GET", "POST"])
@login_required
def topup():
    user = current_user()
    qr_code = None
    amount = None

    if request.method == "POST":
        method = request.form.get("method")

        # 1. TrueMoney
        if method == "truemoney":
            link = request.form.get("truemoney_link", "").strip()
            success, result, voucher_hash = redeem_truemoney(link)

            if not success:
                flash(f"❌ เติมเงินไม่สำเร็จ: {result}", "err")
            else:
                amt = result
                if TopupHistory.query.filter_by(ref=voucher_hash).first():
                    flash("❌ ซองอั่งเปานี้ถูกใช้ไปแล้ว", "err")
                else:
                    user.balance += amt
                    db.session.add(TopupHistory(user_id=user.id, method="truemoney", amount=amt, ref=voucher_hash))
                    db.session.commit()
                    discord_notify("🧧 เติมเงิน TrueMoney สำเร็จ", fields={"ผู้ใช้": user.username, "ยอด": f"{amt:.2f} บาท"})
                    flash(f"✅ เติมเงินสำเร็จ {amt:.2f} บาท", "ok")
                    return redirect(url_for("topup"))

        # 2. PromptPay: สร้าง QR Code
        elif method == "generate_qr":
            try:
                amount = float(request.form.get("promptpay_amount", 0))
                if amount < 10:
                    flash("❌ ขั้นต่ำ 10 บาท", "err")
                    amount = None
                else:
                    payload = promptpay_payload(PROMPTPAY_PHONE, amount)
                    qr_code = qr_data_uri(payload)
            except ValueError:
                flash("❌ กรุณากรอกจำนวนเงินเป็นตัวเลข", "err")

        # 3. PromptPay: ตรวจสอบสลิป
        elif method == "verify_slip":
            file = request.files.get("slip")
            expected = float(request.form.get("expected_amount", 0))
            if not file or not file.filename:
                flash("กรุณาแนบรูปสลิป", "err")
            else:
                try:
                    slip = verify_slip(file.read(), file.filename, expected_amount=expected, receiver_phone=PROMPTPAY_PHONE)

                    if TopupHistory.query.filter_by(ref=slip.trans_ref).first():
                        flash("❌ สลิปนี้ถูกใช้ไปแล้ว", "err")
                    else:
                        user.balance += slip.amount
                        db.session.add(TopupHistory(user_id=user.id, method="promptpay", amount=slip.amount, ref=slip.trans_ref))
                        db.session.commit()
                        discord_notify("🏦 เติมเงินผ่านสลิปสำเร็จ", fields={"ผู้ใช้": user.username, "ยอด": f"{slip.amount:.2f} บาท"})
                        flash(f"✅ เติมเงินผ่านสลิปสำเร็จ {slip.amount:.2f} บาท", "ok")
                        return redirect(url_for("topup"))
                except SlipError as e:
                    flash(f"❌ สลิปไม่ถูกต้อง: {e.user_message}", "err")
                except Exception as e:
                    flash(f"⚠️ เกิดข้อผิดพลาด: {str(e)}", "err")

    history = TopupHistory.query.filter_by(user_id=user.id).order_by(TopupHistory.created_at.desc()).all()
    return render_template("topup.html", history=history, qr_code=qr_code, amount=amount)


@app.route("/contact")
def contact():
    return render_template("contact.html")


# ---------------------------------------------------------------- Checkout & Account
@app.route("/checkout/<package>")
@login_required
def checkout(package):
    if package not in PACKAGES:
        abort(404)
    user = current_user()
    if set(PACKAGES[package]["tracks"]) <= owned_tracks(user):
        flash("คุณมีสิทธิ์ของแพ็กเกจนี้แล้ว", "ok")
        return redirect(url_for("account"))

    Order.query.filter_by(user_id=user.id, package=package, status="pending").update({"status": "expired"})
    order = Order(user_id=user.id, package=package, amount_satang=new_order_amount(PACKAGES[package]["price"]))
    db.session.add(order)
    db.session.commit()
    return redirect(url_for("pay", order_id=order.id))


@app.route("/pay/<int:order_id>", methods=["GET", "POST"])
@login_required
def pay(order_id):
    user = current_user()
    order = Order.query.filter_by(id=order_id, user_id=user.id).first_or_404()

    if order.status == "paid":
        return redirect(url_for("account"))
    if order.status == "pending" and order.created_at < utcnow() - ORDER_TTL:
        order.status = "expired"
        db.session.commit()
    if order.status == "expired":
        flash("ออเดอร์นี้หมดอายุแล้ว กรุณาสั่งใหม่", "err")
        return redirect(url_for("index") + "#packages")

    expected = order.amount_satang / 100
    pay_from_balance = min(user.balance, expected)
    amount_to_pay = round(expected - pay_from_balance, 2)

    # สร้าง QR สำหรับยอดที่ต้องจ่ายจริง (หลังหักเงินในกระเป๋าแล้ว)
    payload = promptpay_payload(PROMPTPAY_PHONE, amount_to_pay if amount_to_pay > 0 else 0.01)
    qr_code = qr_data_uri(payload)

    if request.method == "POST":
        method = request.form.get("method")

        # 1. ชำระด้วยการแนบสลิป
        if method == "verify_slip":
            file = request.files.get("slip")
            if not file or not file.filename:
                flash("กรุณาแนบรูปสลิป", "err")
            else:
                try:
                    slip = verify_slip(file.read(), file.filename, expected_amount=amount_to_pay, receiver_phone=PROMPTPAY_PHONE)
                    if Order.query.filter_by(slip_ref=slip.trans_ref).first():
                        raise SlipError("duplicate", "สลิปนี้ถูกใช้ไปแล้ว")

                    user.balance -= pay_from_balance
                    order.status = "paid"
                    order.paid_at = utcnow()
                    order.slip_ref = slip.trans_ref
                    db.session.commit()

                    discord_notify("💰 มีออเดอร์ใหม่ชำระแล้ว", fields={"แพ็กเกจ": PACKAGES[order.package]["name"], "ยอด": f"{expected:,.2f} บาท", "ผู้ซื้อ": user.username, "ออเดอร์": f"#{order.id}"})
                    flash("ชำระเงินสำเร็จ เริ่มเรียนได้เลย!", "ok")
                    return redirect(url_for("account"))
                except SlipError as e:
                    flash(e.user_message, "err")
                except IntegrityError:
                    db.session.rollback()
                    flash("สลิปนี้ถูกใช้ไปแล้ว", "err")

        # 2. ชำระด้วย TrueMoney
        elif method == "truemoney":
            link = request.form.get("truemoney_link", "").strip()
            success, result, voucher_hash = redeem_truemoney(link)

            if not success:
                flash(f"❌ เติมเงินไม่สำเร็จ: {result}", "err")
            else:
                if Order.query.filter_by(slip_ref=voucher_hash).first():
                    flash("❌ ซองอั่งเปานี้ถูกใช้ไปแล้ว", "err")
                else:
                    change = result - amount_to_pay
                    if change > 0:
                        user.balance += change

                    user.balance -= pay_from_balance
                    order.status = "paid"
                    order.paid_at = utcnow()
                    order.slip_ref = voucher_hash
                    db.session.commit()

                    discord_notify("💰 มีออเดอร์ใหม่ชำระแล้ว (TrueMoney)", fields={"แพ็กเกจ": PACKAGES[order.package]["name"], "ยอด": f"{expected:,.2f} บาท", "ผู้ซื้อ": user.username, "ออเดอร์": f"#{order.id}"})
                    flash(f"✅ ชำระเงินสำเร็จ (ทอนเงินคืนกระเป๋า {change:.2f} บาท)", "ok")
                    return redirect(url_for("account"))

    return render_template("pay.html", order=order, package=PACKAGES[order.package], amount=expected, amount_to_pay=amount_to_pay, pay_from_balance=pay_from_balance, qr=qr_code)


@app.route("/account")
@login_required
def account():
    user = current_user()
    orders = Order.query.filter_by(user_id=user.id).order_by(Order.created_at.desc()).all()
    enrollments = Enrollment.query.filter_by(user_id=user.id).order_by(Enrollment.created_at.desc()).all()
    return render_template(
        "account.html",
        orders=orders,
        packages=PACKAGES,
        owned=sorted(owned_tracks(user)),
        enrollments=enrollments,
    )


# สร้างตารางฐานข้อมูลอัตโนมัติเมื่อเริ่มรัน (รวมถึงบน Vercel ด้วย)
with app.app_context():
    db.create_all()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
