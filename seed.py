"""ใส่บทเรียนตัวอย่างลงฐานข้อมูล รันครั้งเดียว: python seed.py
แก้ video_url เป็นลิงก์ embed ของ YouTube แบบ unlisted เช่น
https://www.youtube-nocookie.com/embed/VIDEO_ID
"""
from app import Lesson, app, db

PLACEHOLDER = "https://www.youtube-nocookie.com/embed/REPLACE_ME"

# (track, ลำดับ, ชื่อบทเรียน, ลิงก์วิดีโอ, ทดลองเรียนฟรีหรือไม่)
LESSONS = [
    ("basic", 1, "ทำความรู้จัก CapCut PC และเริ่มโปรเจกต์แรก", PLACEHOLDER, True),
    ("basic", 2, "ตัดคลิปและลบส่วนที่ไม่จำเป็น", PLACEHOLDER, False),
    ("basic", 3, "Transition และ Text พื้นฐาน", PLACEHOLDER, False),
    ("basic", 4, "ปรับเสียงและ Export ให้คมชัด", PLACEHOLDER, False),
    ("advanced", 1, "จังหวะการตัด (Rhythm & Pacing)", PLACEHOLDER, True),
    ("advanced", 2, "Keyframe และการเคลื่อนไหว", PLACEHOLDER, False),
    ("advanced", 3, "Color Grading สร้างสไตล์ของตัวเอง", PLACEHOLDER, False),
    ("advanced", 4, "Sound Design เลือกเพลงให้ตรงอารมณ์", PLACEHOLDER, False),
]

with app.app_context():
    db.create_all()
    if Lesson.query.count() == 0:
        for track, pos, title, url, free in LESSONS:
            db.session.add(
                Lesson(track=track, position=pos, title=title, video_url=url, is_free=free)
            )
        db.session.commit()
        print("ใส่บทเรียนเรียบร้อย")
    else:
        print("มีบทเรียนอยู่แล้ว ข้ามการใส่ข้อมูล")
