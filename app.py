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
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    GOOGLE_CLIENT_ID=os.getenv("GOOGLE_CLIENT_ID", ""),
    GOOGLE_CLIENT_SECRET=os.getenv("GOOGLE_CLIENT_SECRET", ""),
    PROMPTPAY_PHONE=os.getenv("PROMPTPAY_PHONE", ""),
    ADMIN_EMAIL=os.getenv("ADMIN_EMAIL", ""),
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=bool(os.getenv("VERCEL")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgres://"):
    app.config["SQLALCHEMY_DATABASE_URI"] = app.config["SQLALCHEMY_DATABASE_URI"].replace("postgres://", "postgresql://", 1)

db = SQLAlchemy(app)

SHOP_NAME = os.getenv("SHOP_NAME", "CapCut PC Course")
PROMPTPAY_PHONE = os.getenv("PROMPTPAY_PHONE", "")
ORDER_TTL = timedelta(minutes=30)

ADMIN_USERNAMES = {
    name.strip().lower()
    for name in (os.getenv("ADMIN_EMAIL", "") + "," + os.getenv("DEV_USERNAME", "")).split(",")
    if name.strip()
}

PACKAGES = {
    "basic": {"name": "Basic", "price": 10, "tracks": ["basic"], "description": "ตัดต่อพื้นฐาน ตัดคลิปจนส่งออกได้เองทุกขั้นตอน"},
    "advanced": {"name": "Advanced", "price": 890, "tracks": ["advanced"], "description": "จังหวะ คีย์เฟรม สี และเสียง ทำให้งานดูโปร"},
    "bundle": {"name": "Bundle", "price": 1090, "tracks": ["basic", "advanced"], "description": "ครบทั้ง Basic และ Advanced ประหยัดกว่าซื้อแยก"},
}

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    balance = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=utcnow)

class Lesson(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    track = db.Column(db.String(20), nullable=False)
    position = db.Column(db.Integer, default=0)
    title = db.Column(db.String(150), nullable=False)
    video_url = db.Column(db.String(300), nullable=False)
    is_free = db.Column(db.Boolean, default=False)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    package = db.Column(db.String(20), nullable=False)
    amount_satang = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(10), default="pending")
    created_at = db.Column(db.DateTime, default=utcnow)
    paid_at = db.Column(db.DateTime)
    slip_ref = db.Column(db.String(100), unique=True)

class Enrollment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lesson.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    __table_args__ = (db.UniqueConstraint("user_id", "lesson_id", name="uq_enroll"),)
    lesson = db.relationship("Lesson", backref="enrollments")

class TopupHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    method = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    ref = db.Column(db.String(100), unique=True)
    created_at = db.Column(db.DateTime, default=utcnow)

class SiteSetting(db.Model):
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Text, nullable=False)

class UserProfile(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    avatar_url = db.Column(db.Text, default="")
    banner = db.Column(db.Text, default="")
    accent = db.Column(db.String(7), default="#2563eb")

class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    sender = db.Column(db.String(10), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    is_read = db.Column(db.Boolean, default=False)
    user = db.relationship("User", backref="chat_messages")

def get_setting(key, default=""):
    setting = db.session.get(SiteSetting, key)
    return setting.value if setting else default

def set_setting(key, value):
    setting = db.session.get(SiteSetting, key)
    if setting: setting.value = value
    else: db.session.add(SiteSetting(key=key, value=value))
    db.session.commit()

def current_user():
    uid = session.get("uid")
    return db.session.get(User, uid) if uid else None

def owned_tracks(user):
    if not user: return set()
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

def is_admin_user(user):
    return bool(user) and user.username.strip().lower() in ADMIN_USERNAMES

def clamp_int(raw, lo, hi, default):
    try: value = int(float(raw))
    except (TypeError, ValueError): return default
    return max(lo, min(hi, value))

def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            flash("กรุณาเข้าสู่ระบบก่อน", "err")
            return redirect(url_for("login"))
        if not is_admin_user(user):
            flash("❌ คุณไม่มีสิทธิ์เข้าถึงหน้านี้", "err")
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapper

def new_order_amount(price_baht):
    return int(price_baht * 100)

def qr_data_uri(text):
    buf = io.BytesIO()
    qrcode.make(text).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_ACCENT = "#2563eb"

def profile_for(user):
    if not user: return {"name": "", "initial": "", "avatar_url": "", "banner": "", "accent": DEFAULT_ACCENT, "accent_soft": DEFAULT_ACCENT + "1f"}
    p = db.session.get(UserProfile, user.id)
    accent = p.accent if p and p.accent else DEFAULT_ACCENT
    name = user.username.split("@")[0]
    return {"name": name, "initial": (name[:1] or "?").upper(), "avatar_url": p.avatar_url if p and p.avatar_url else "", "banner": p.banner if p and p.banner else "", "accent": accent, "accent_soft": accent + "1f"}

def user_extras(user):
    p = db.session.get(UserProfile, user.id)
    return {"email": user.username if "@" in user.username else "", "avatar": p.avatar_url if p and p.avatar_url else "", "banner": p.banner if p and p.banner else "", "created": user.created_at}

@app.context_processor
def inject_globals():
    user = current_user()
    return {
        "user": user, "shop_name": SHOP_NAME, "is_admin": is_admin_user(user),
        "hero_title": get_setting("hero_title", "เรียนตัดต่อ CapCut PC ตั้งแต่ศูนย์"),
        "hero_subtitle": get_setting("hero_subtitle", "จนทำงานได้สไตล์ของตัวเอง ทดลองเรียนฟรีได้ทุกบทแรก"),
        "hero_img": get_setting("hero_img", ""), "btn_text": get_setting("btn_text", "ดูแพ็กเกจเลย"),
        "btn_size": f"{get_setting('btn_pad_y', '14')}px {get_setting('btn_pad_x', '32')}px",
        "btn_font_size": f"{get_setting('btn_font_size', '16')}px", "btn_radius": f"{get_setting('btn_radius', '12')}px",
        "profile": profile_for(user), "google_client_id": app.config["GOOGLE_CLIENT_ID"],
    }

@app.errorhandler(413)
def file_too_large(_e):
    flash("ไฟล์รูปใหญ่เกิน 10 MB กรุณาลดขนาดรูปภาพ", "err")
    return redirect(request.referrer or url_for("index"))

@app.route("/admin")
@admin_required
def admin_dashboard():
    def topup_sum(method=None):
        q = db.session.query(db.func.coalesce(db.func.sum(TopupHistory.amount), 0.0))
        if method: q = q.filter(TopupHistory.method == method)
        return float(q.scalar() or 0)
    def topup_count(method=None):
        q = TopupHistory.query
        if method: q = q.filter_by(method=method)
        return q.count()

    order_total = db.session.query(db.func.coalesce(db.func.sum(Order.amount_satang), 0)).filter_by(status="paid").scalar() / 100
    order_count = Order.query.filter_by(status="paid").count()

    paid_rows = db.session.query(Order, User).join(User, User.id == Order.user_id).filter(Order.status == "paid").order_by(Order.paid_at.desc()).limit(50).all()
    paid_orders = [{"paid_at": o.paid_at, "username": u.username, "package": PACKAGES[o.package]["name"], "amount": o.amount_satang / 100} for o, u in paid_rows]

    histories = TopupHistory.query.order_by(TopupHistory.created_at.desc()).limit(50).all()
    btn = {"pad_y": get_setting("btn_pad_y", "14"), "pad_x": get_setting("btn_pad_x", "32"), "font_size": get_setting("btn_font_size", "16"), "radius": get_setting("btn_radius", "12")}
    
    chat_users = db.session.query(User).join(ChatMessage).group_by(User.id).order_by(db.func.max(ChatMessage.created_at).desc()).all()

    return render_template("admin.html", total=topup_sum(), tm=topup_sum("truemoney"), pp=topup_sum("promptpay"),
        tm_count=topup_count("truemoney"), pp_count=topup_count("promptpay"), order_total=order_total, order_count=order_count,
        paid_orders=paid_orders, histories=histories, user_count=User.query.count(), btn=btn, chat_users=chat_users)

@app.route("/admin/settings", methods=["POST"])
@admin_required
def admin_settings():
    set_setting("hero_title", request.form.get("hero_title", "").strip())
    set_setting("hero_subtitle", request.form.get("hero_subtitle", "").strip())
    set_setting("btn_text", request.form.get("btn_text", "").strip() or "ดูแพ็กเกจเลย")
    set_setting("btn_pad_y", str(clamp_int(request.form.get("btn_pad_y"), 0, 60, 14)))
    set_setting("btn_pad_x", str(clamp_int(request.form.get("btn_pad_x"), 0, 120, 32)))
    set_setting("btn_font_size", str(clamp_int(request.form.get("btn_font_size"), 10, 40, 16)))
    set_setting("btn_radius", str(clamp_int(request.form.get("btn_radius"), 0, 60, 12)))
    if request.form.get("remove_hero_img"): set_setting("hero_img", "")
    file = request.files.get("hero_img")
    if file and file.filename:
        mime = file.mimetype or ""
        if not mime.startswith("image/"): return redirect(url_for("admin_dashboard"))
        b64 = base64.b64encode(file.read()).decode("utf-8")
        set_setting("hero_img", f"data:{mime};base64,{b64}")
    flash("✅ อัปเดตหน้าเว็บเรียบร้อยแล้ว", "ok")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/users")
@admin_required
def admin_users():
    q = (request.args.get("q") or "").strip()
    query = User.query
    if q: query = query.filter(User.username.ilike(f"%{q}%"))
    users = query.order_by(User.id.desc()).all()
    rows = [{"id": u.id, "username": u.username, "balance": float(u.balance or 0), "email": user_extras(u)["email"], "avatar": user_extras(u)["avatar"], "created": user_extras(u)["created"]} for u in users]
    return render_template("admin_users.html", user=current_user(), users=rows, q=q)

@app.route("/admin/users/<int:user_id>")
@admin_required
def admin_user_detail(user_id):
    target = db.get_or_404(User, user_id)
    histories = TopupHistory.query.filter_by(user_id=target.id).order_by(TopupHistory.created_at.desc()).limit(50).all()
    return render_template("admin_user_detail.html", user=current_user(), u=target, info=user_extras(target), histories=histories)

@app.route("/admin/users/<int:user_id>/balance", methods=["POST"])
@admin_required
def admin_adjust_balance(user_id):
    target = db.get_or_404(User, user_id)
    back = url_for("admin_user_detail", user_id=user_id)
    try: amount = round(float(request.form.get("amount", "0")), 2)
    except ValueError: amount = 0
    op = request.form.get("op")
    if not (0 < amount < 1_000_000_000) or op not in ("add", "subtract"): return redirect(back)
    current = float(target.balance or 0)
    if op == "subtract":
        if amount > current: return redirect(back)
        signed = -amount
    else: signed = amount
    target.balance = round(current + signed, 2)
    db.session.add(TopupHistory(user_id=target.id, method="admin", amount=signed))
    db.session.commit()
    return redirect(back)

# ---------------------------------------------------------------- Chat API
@app.route("/api/chat/send", methods=["POST"])
@login_required
def api_chat_send():
    user = current_user()
    data = request.get_json(silent=True) or {}
    msg = data.get("message", "").strip()
    if not msg: return jsonify(ok=False, error="empty_message"), 400
    
    target_id = data.get("target_id")
    sender = "admin" if is_admin_user(user) else "user"
    actual_user_id = int(target_id) if sender == "admin" and target_id else user.id
    
    new_msg = ChatMessage(user_id=actual_user_id, sender=sender, message=msg)
    db.session.add(new_msg)
    db.session.commit()
    return jsonify(ok=True)

@app.route("/api/chat/messages")
@login_required
def api_chat_messages():
    user = current_user()
    target_id = request.args.get("user_id")
    if is_admin_user(user) and target_id: actual_id = int(target_id)
    else: actual_id = user.id
    
    msgs = ChatMessage.query.filter_by(user_id=actual_id).order_by(ChatMessage.created_at.asc()).all()
    if is_admin_user(user):
        ChatMessage.query.filter_by(user_id=actual_id, sender="user", is_read=False).update({"is_read": True})
        db.session.commit()
        
    return jsonify(ok=True, messages=[{"sender": m.sender, "message": m.message, "time": m.created_at.strftime("%H:%M")} for m in msgs])

@app.route("/api/chat/users")
@admin_required
def api_chat_users():
    users_with_chats = db.session.query(User).join(ChatMessage).group_by(User.id).order_by(db.func.max(ChatMessage.created_at).desc()).all()
    return jsonify(ok=True, users=[{"id": u.id, "username": u.username.split('@')[0], "full": u.username} for u in users_with_chats])

@app.route("/admin/chats")
@admin_required
def admin_chats():
    users_with_chats = db.session.query(User).join(ChatMessage).group_by(User.id).order_by(db.func.max(ChatMessage.created_at).desc()).all()
    return render_template("admin_chats.html", chat_users=users_with_chats)

# ---------------------------------------------------------------- Profile API
@app.route("/profile/accent", methods=["POST"])
@login_required
def profile_accent():
    data = request.get_json(silent=True) or {}
    accent = str(data.get("accent", "")).strip()
    if HEX_RE.match(accent):
        p = db.session.get(UserProfile, current_user().id) or UserProfile(user_id=current_user().id)
        p.accent = accent.lower(); db.session.add(p); db.session.commit()
        return jsonify(ok=True)
    return jsonify(ok=False), 400

@app.route("/profile/banner", methods=["POST"])
@login_required
def profile_banner():
    user = current_user()
    p = db.session.get(UserProfile, user.id) or UserProfile(user_id=user.id)
    data = request.get_json(silent=True) or {}
    if data.get("remove"): p.banner = ""
    elif data.get("image"):
        if not data["image"].startswith("data:image/"): return jsonify(ok=False, error="invalid_image"), 400
        p.banner = data["image"]
    else: return jsonify(ok=False, error="no_file"), 400
    db.session.add(p); db.session.commit()
    return jsonify(ok=True)

@app.route("/profile/avatar", methods=["POST"])
@login_required
def profile_avatar():
    user = current_user()
    p = db.session.get(UserProfile, user.id) or UserProfile(user_id=user.id)
    data = request.get_json(silent=True) or {}
    if data.get("remove"): p.avatar_url = ""
    elif data.get("image"):
        if not data["image"].startswith("data:image/"): return jsonify(ok=False, error="invalid_image"), 400
        p.avatar_url = data["image"]
    else: return jsonify(ok=False, error="no_file"), 400
    db.session.add(p); db.session.commit()
    return jsonify(ok=True)

# ---------------------------------------------------------------- Pages
@app.route("/")
def index():
    user = current_user()
    lessons = Lesson.query.order_by(Lesson.track, Lesson.position).all()
    return render_template("index.html", packages=PACKAGES, lessons=lessons, owned=owned_tracks(user))

@app.route("/free")
def free_lessons():
    lessons = Lesson.query.filter_by(is_free=True).order_by(Lesson.track, Lesson.position).all()
    return render_template("free.html", lessons=lessons)

@app.route("/lesson/<int:lesson_id>")
def lesson(lesson_id):
    user = current_user()
    if not user: return redirect(url_for("login", next=request.path))
    item = db.get_or_404(Lesson, lesson_id)
    if item.is_free:
        if not Enrollment.query.filter_by(user_id=user.id, lesson_id=item.id).first():
            db.session.add(Enrollment(user_id=user.id, lesson_id=item.id)); db.session.commit()
        return render_template("lesson.html", lesson=item)
    if item.track not in owned_tracks(user): return redirect(url_for("index") + "#packages")
    return render_template("lesson.html", lesson=item)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        if User.query.filter_by(username=username).first():
            flash("❌ Username นี้ถูกใช้งานแล้ว", "err")
        else:
            user = User(username=username, password_hash=generate_password_hash(password))
            db.session.add(user); db.session.commit()
            session.clear(); session["uid"] = user.id
            return redirect(url_for("index"))
    return render_template("auth.html", mode="register")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session.clear(); session["uid"] = user.id; session.permanent = True
            return redirect(request.values.get("next", url_for("index")))
        flash("Username หรือรหัสผ่านไม่ถูกต้อง", "err")
    return render_template("auth.html", mode="login")

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("index"))

@app.route("/topup", methods=["GET", "POST"])
@login_required
def topup():
    user = current_user(); qr_code = None; amount = None
    if request.method == "POST":
        method = request.form.get("method")
        if method == "truemoney":
            success, result, voucher_hash = redeem_truemoney(request.form.get("truemoney_link", "").strip())
            if success and not TopupHistory.query.filter_by(ref=voucher_hash).first():
                user.balance += result
                db.session.add(TopupHistory(user_id=user.id, method="truemoney", amount=result, ref=voucher_hash)); db.session.commit()
                return redirect(url_for("topup"))
        elif method == "generate_qr":
            try:
                amount = float(request.form.get("promptpay_amount", 0))
                if amount >= 10: qr_code = qr_data_uri(promptpay_payload(PROMPTPAY_PHONE, amount))
            except ValueError: pass
        elif method == "verify_slip":
            file = request.files.get("slip")
            if file:
                try:
                    slip = verify_slip(file.read(), file.filename, expected_amount=float(request.form.get("expected_amount", 0)), receiver_phone=PROMPTPAY_PHONE)
                    if not TopupHistory.query.filter_by(ref=slip.trans_ref).first():
                        user.balance += slip.amount
                        db.session.add(TopupHistory(user_id=user.id, method="promptpay", amount=slip.amount, ref=slip.trans_ref)); db.session.commit()
                        return redirect(url_for("topup"))
                except: pass
    history = TopupHistory.query.filter_by(user_id=user.id).order_by(TopupHistory.created_at.desc()).all()
    return render_template("topup.html", history=history, qr_code=qr_code, amount=amount)

@app.route("/contact")
def contact():
    return render_template("contact.html")

@app.route("/checkout/<package>")
@login_required
def checkout(package):
    if package not in PACKAGES: abort(404)
    user = current_user()
    Order.query.filter_by(user_id=user.id, package=package, status="pending").update({"status": "expired"})
    order = Order(user_id=user.id, package=package, amount_satang=new_order_amount(PACKAGES[package]["price"]))
    db.session.add(order); db.session.commit()
    return redirect(url_for("pay", order_id=order.id))

@app.route("/pay/<int:order_id>", methods=["GET", "POST"])
@login_required
def pay(order_id):
    user = current_user()
    order = Order.query.filter_by(id=order_id, user_id=user.id).first_or_404()
    if order.status == "paid": return redirect(url_for("account"))
    if order.status == "expired": return redirect(url_for("index") + "#packages")
    
    price = PACKAGES[order.package]["price"]
    expected = order.amount_satang / 100
    balance = float(user.balance or 0)
    enough = balance >= price
    pay_from_balance = price if enough else balance
    amount_to_pay = 0.0 if enough else round(expected - balance, 2)
    qr_code = None if enough else qr_data_uri(promptpay_payload(PROMPTPAY_PHONE, amount_to_pay))
    
    if request.method == "POST":
        method = request.form.get("method")
        if method == "pay_with_balance" and enough and request.form.get("agree"):
            user.balance = round(balance - price, 2); order.amount_satang = price * 100
            order.status = "paid"; order.paid_at = utcnow(); db.session.commit()
            return redirect(url_for("account"))
        elif method == "verify_slip":
            file = request.files.get("slip")
            if file:
                try:
                    slip = verify_slip(file.read(), file.filename, expected_amount=amount_to_pay, receiver_phone=PROMPTPAY_PHONE)
                    if not Order.query.filter_by(slip_ref=slip.trans_ref).first():
                        user.balance -= pay_from_balance
                        order.status = "paid"; order.paid_at = utcnow(); order.slip_ref = slip.trans_ref; db.session.commit()
                        return redirect(url_for("account"))
                except: pass
    return render_template("pay.html", order=order, package=PACKAGES[order.package], price=price, amount=expected, amount_to_pay=amount_to_pay, pay_from_balance=pay_from_balance, qr=qr_code, enough=enough)

@app.route("/account")
@login_required
def account():
    user = current_user()
    orders = Order.query.filter_by(user_id=user.id).order_by(Order.created_at.desc()).all()
    enrollments = Enrollment.query.filter_by(user_id=user.id).order_by(Enrollment.created_at.desc()).all()
    
    # ดึงข้อมูลบทเรียนมาแนบกับ Enrollment เพื่อให้หน้าเว็บเรียกใช้ e.lesson.title ได้
    enrollment_data = []
    for e in enrollments:
        lesson = db.session.get(Lesson, e.lesson_id)
        if lesson:
            enrollment_data.append({
                "title": lesson.title,
                "created_at": e.created_at
            })

    return render_template("account.html", orders=orders, packages=PACKAGES, owned=sorted(owned_tracks(user)), enrollments=enrollment_data)

def ensure_dev_account():
    dev_user = os.getenv("DEV_USERNAME", "").strip().lower()
    dev_pass = os.getenv("DEV_PASSWORD", "")
    if dev_user and dev_pass:
        user = User.query.filter_by(username=dev_user).first()
        if not user: db.session.add(User(username=dev_user, password_hash=generate_password_hash(dev_pass)))
        elif not check_password_hash(user.password_hash, dev_pass): user.password_hash = generate_password_hash(dev_pass)
        db.session.commit()

with app.app_context():
    db.create_all()
    ensure_dev_account()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
