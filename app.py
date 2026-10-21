import base64
import io
import os
import random
import secrets
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
    # ใช้ DATABASE_URL จาก Environment Variable (เช่น Neon PostgreSQL)
    SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///shop.db"),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    GOOGLE_CLIENT_ID=os.getenv("GOOGLE_CLIENT_ID", ""),
    GOOGLE_CLIENT_SECRET=os.getenv("GOOGLE_CLIENT_SECRET", ""),
)

# แก้ไข URL ของ PostgreSQL ให้ Flask-SQLAlchemy รู้จัก (บางที Vercel ให้มาเป็น postgres://)
if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgres://"):
    app.config["SQLALCHEMY_DATABASE_URI"] = app.config["SQLALCHEMY_DATABASE_URI"].replace("postgres://", "postgresql://", 1)

db = SQLAlchemy(app)

SHOP_NAME = os.getenv("SHOP_NAME", "CapCut PC Course")
PROMPTPAY_PHONE = os.getenv("PROMPTPAY_PHONE", "")
ORDER_TTL = timedelta(minutes=30)

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
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    balance = db.Column(db.Float, default=0.0)  # เพิ่มระบบกระเป๋าเงิน
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
    # ยอดเป็นสตางค์ เช่น 490.37 บาท = 49037 ใช้แยกออเดอร์ที่กำลังรอจ่าย
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


# ---------------------------------------------------------------- Helpers
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
    return {"user": current_user(), "shop_name": SHOP_NAME}


@app.errorhandler(413)
def file_too_large(_e):
    flash("ไฟล์รูปใหญ่เกิน 5 MB กรุณาลดขนาดรูปภาพ", "err")
    return redirect(request.referrer or url_for("index"))


# ---------------------------------------------------------------- Pages
@app.route("/")
def index():
    user = current_user()
    lessons = Lesson.query.order_by(Lesson.track, Lesson.position).all()
    return render_template(
        "index.html",
        packages=PACKAGES,
        lessons=lessons,
        owned=owned_tracks(user),
    )


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
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if "@" not in email or len(password) < 6:
            flash("อีเมลไม่ถูกต้อง หรือรหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร", "err")
        elif User.query.filter_by(email=email).first():
            flash("อีเมลนี้ถูกใช้งานแล้ว กรุณาใช้อีเมลอื่น", "err")
        else:
            user = User(email=email, password_hash=generate_password_hash(password))
            db.session.add(user)
            db.session.commit()
            session.clear()
            session["uid"] = user.id
            flash("สมัครสมาชิกสำเร็จ ยินดีต้อนรับ!", "ok")
            nxt = request.args.get("next")
            return redirect(nxt or url_for("index"))
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            session.clear()
            session["uid"] = user.id
            flash("เข้าสู่ระบบสำเร็จ", "ok")
            nxt = request.args.get("next")
            return redirect(nxt or url_for("index"))

        flash("อีเมลหรือรหัสผ่านไม่ถูกต้อง", "err")
    return render_template("auth.html", mode="login")


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
        # สร้าง Flow เพื่อแลก Code เป็น Token
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
        # บังคับ redirect_uri ให้ตรงกับที่ตั้งค่าไว้ (ใช้ 'postmessage' สำหรับ popup)
        flow.redirect_uri = "postmessage"
        
        # แลก Code เป็น Token
        flow.fetch_token(code=code)
        
        # ดึงข้อมูลจาก ID Token
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

        # ค้นหาหรือสร้าง User ใหม่
        user = User.query.filter_by(email=email).first()
        if not user:
            user = User(
                email=email,
                password_hash=generate_password_hash(secrets.token_hex(16)),
            )
            db.session.add(user)
            db.session.commit()

        session.clear()
        session["uid"] = user.id
        return jsonify(ok=True, redirect=url_for("index"))

    except Exception as e:
        import traceback
        traceback.print_exc() # ปริ้นท์ error ลงใน Logs ของ Vercel เพื่อดูสาเหตุจริงๆ
        return jsonify(ok=False, error=str(e)), 401 # ส่งข้อความ Error กลับไปให้หน้าเว็บเห็น


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
    if request.method == "POST":
        method = request.form.get("method")
        
        if method == "truemoney":
            link = request.form.get("truemoney_link", "").strip()
            success, result, voucher_hash = redeem_truemoney(link)
            
            if not success:
                flash(f"❌ เติมเงินไม่สำเร็จ: {result}", "err")
            else:
                amount = result
                # เช็กว่าซองนี้เคยใช้หรือยัง
                if TopupHistory.query.filter_by(ref=voucher_hash).first():
                    flash("❌ ซองอั่งเปานี้ถูกใช้ไปแล้ว", "err")
                else:
                    user.balance += amount
                    db.session.add(TopupHistory(user_id=user.id, method="truemoney", amount=amount, ref=voucher_hash))
                    db.session.commit()
                    discord_notify("🧧 เติมเงิน TrueMoney สำเร็จ", fields={"ผู้ใช้": user.email, "ยอด": f"{amount:.2f} บาท"})
                    flash(f"✅ เติมเงินสำเร็จ {amount:.2f} บาท", "ok")
                    return redirect(url_for("topup"))
                    
        elif method == "promptpay":
            amount = float(request.form.get("promptpay_amount", 0))
            if amount < 10:
                flash("❌ ขั้นต่ำ 10 บาท", "err")
            else:
                flash("⚠️ ระบบเติมเงินผ่าน PromptPay QR อยู่ในช่วงพัฒนา กรุณาใช้ TrueMoney ก่อน", "err")
                
    history = TopupHistory.query.filter_by(user_id=user.id).order_by(TopupHistory.created_at.desc()).all()
    return render_template("topup.html", history=history)


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

    if request.method == "POST":
        file = request.files.get("slip")
        if not file or not file.filename:
            flash("กรุณาเลือกรูปสลิป", "err")
        else:
            try:
                slip = verify_slip(file.read(), file.filename, expected_amount=expected, receiver_phone=PROMPTPAY_PHONE)
                if Order.query.filter_by(slip_ref=slip.trans_ref).first():
                    raise SlipError("duplicate", "สลิปนี้ถูกใช้ไปแล้ว")
                
                order.status = "paid"
                order.paid_at = utcnow()
                order.slip_ref = slip.trans_ref
                db.session.commit()
            except SlipError as e:
                db.session.rollback()
                discord_notify("⚠️ สลิปไม่ผ่านการตรวจ", color=0xFF5C7A, fields={"รหัส": e.code, "ออเดอร์": f"#{order.id}", "ยอด": f"{expected:.2f}"})
                flash(e.user_message, "err")
            except IntegrityError:
                db.session.rollback()
                flash("สลิปนี้ถูกใช้ไปแล้ว", "err")
            else:
                discord_notify("💰 มีออเดอร์ใหม่ชำระแล้ว", fields={"แพ็กเกจ": PACKAGES[order.package]["name"], "ยอด": f"{expected:,.2f} บาท", "ผู้ซื้อ": user.email, "ออเดอร์": f"#{order.id}"})
                flash("ชำระเงินสำเร็จ เริ่มเรียนได้เลย!", "ok")
                return redirect(url_for("account"))

    payload = promptpay_payload(PROMPTPAY_PHONE, expected)
    return render_template("pay.html", order=order, package=PACKAGES[order.package], amount=expected, qr=qr_data_uri(payload))


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
