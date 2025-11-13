# main.py (Flask-SQLAlchemy ORM 統合版 - Render対応/安定化)
import psycopg2
import os
from datetime import datetime, date, timedelta, time, timedelta
from flask import Flask, render_template, request, url_for, jsonify, redirect, flash, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, text, inspect
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.exc import IntegrityError  # ここでインポート
from sqlalchemy.orm import aliased
from functools import wraps

# =========================================================================
# アプリ / DB 設定
# =========================================================================
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'default_secret_key_for_dev')
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///school3.db')
# DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://user:password@localhost/dbname')
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# Render の旧形式対策
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# 接続が切れたソケットを自動復帰（Render/PGで便利）
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_pre_ping": True}

db = SQLAlchemy(app)

# =========================================================================
# 出席判定定数
# =========================================================================
ABSENT_THRESHOLD_MINUTES = 20   # 授業開始+20分で欠席扱い
LATE_THRESHOLD_MINUTES   = 10   # 授業開始+10分で遅刻扱い

# =========================================================================
# スキーマ定義
#   ※ 複合PKは「列側で primary_key=True」に統一
#   ※ DBの現在時刻は server_default=func.now()（SQLite/PG両対応）
# =========================================================================

class 曜日マスタ(db.Model):
    __tablename__ = '曜日マスタ'
    曜日ID = db.Column(db.SmallInteger, primary_key=True)
    曜日名 = db.Column(db.String(10), nullable=False)
    備考  = db.Column(db.String(50))


class 期マスタ(db.Model):
    __tablename__ = '期マスタ'
    期ID = db.Column(db.SmallInteger, primary_key=True)
    期名 = db.Column(db.String(32), nullable=False)
    備考 = db.Column(db.String(50))


class 学科(db.Model):
    __tablename__ = '学科'
    学科ID = db.Column(db.SmallInteger, primary_key=True)
    学科名 = db.Column(db.String(32))
    備考  = db.Column(db.String(50))


class 教室(db.Model):
    __tablename__ = '教室'
    教室ID   = db.Column(db.SmallInteger, primary_key=True)
    教室名   = db.Column(db.String(32), nullable=False)
    収容人数 = db.Column(db.SmallInteger, nullable=False)
    備考    = db.Column(db.String(50))


class 授業科目(db.Model):
    __tablename__ = '授業科目'
    授業科目ID = db.Column(db.SmallInteger, primary_key=True)
    授業科目名 = db.Column(db.String(32), nullable=False)
    学科ID     = db.Column(db.SmallInteger, db.ForeignKey('学科.学科ID'), nullable=False)
    単位       = db.Column(db.SmallInteger, nullable=False)
    学科フラグ = db.Column(db.SmallInteger, nullable=False)
    備考       = db.Column(db.String(50))
    学科 = db.relationship('学科', backref=db.backref('授業科目_list', lazy=True))


class 生徒(db.Model):
    __tablename__ = '生徒'
    # ※ 複合主キー：列側に primary_key=True を付与（__table_args__での重複宣言はしない）
    学科ID   = db.Column(db.SmallInteger, db.ForeignKey('学科.学科ID'), primary_key=True, nullable=False)
    学生番号 = db.Column(db.Integer, primary_key=True, nullable=False)
    生徒名   = db.Column(db.Text, nullable=False)
    備考     = db.Column(db.Text)
    学科 = db.relationship('学科', backref=db.backref('生徒_list', lazy=True))


class TimeTable(db.Model):
    __tablename__ = 'TimeTable'
    時限   = db.Column(db.SmallInteger, primary_key=True)
    開始時刻 = db.Column(db.Time, nullable=False)
    終了時刻 = db.Column(db.Time, nullable=False)
    備考    = db.Column(db.Text)


class 週時間割(db.Model):
    __tablename__ = '週時間割'
    年度  = db.Column(db.Integer, primary_key=True)
    学科ID = db.Column(db.SmallInteger, db.ForeignKey('学科.学科ID'), primary_key=True)
    期    = db.Column(db.SmallInteger, db.ForeignKey('期マスタ.期ID'), primary_key=True)
    曜日  = db.Column(db.SmallInteger, db.ForeignKey('曜日マスタ.曜日ID'), primary_key=True)
    時限  = db.Column(db.SmallInteger, db.ForeignKey('TimeTable.時限'), primary_key=True)
    科目ID  = db.Column(db.SmallInteger, db.ForeignKey('授業科目.授業科目ID'))
    教室ID  = db.Column(db.SmallInteger, db.ForeignKey('教室.教室ID'))
    備考    = db.Column(db.String(50))
    曜日マスタ = db.relationship('曜日マスタ', backref=db.backref('時間割_list', lazy=True))
    授業科目   = db.relationship('授業科目', backref=db.backref('時間割_list', lazy=True))
    教室      = db.relationship('教室', backref=db.backref('時間割_list', lazy=True))
    期マスタ   = db.relationship('期マスタ', backref=db.backref('週時間割_list', lazy=True))
    TimeTable = db.relationship('TimeTable', backref=db.backref('週時間割_list', lazy=True))


class 入退室(db.Model):
    __tablename__ = '入退室'
    記録ID   = db.Column(db.Integer, primary_key=True, autoincrement=True)
    学生番号 = db.Column(db.Integer, nullable=False)
    生徒名   = db.Column(db.String(32), nullable=False)
    入退出時間 = db.Column(db.DateTime(timezone=True), server_default=func.now(), nullable=False)
    入室区分 = db.Column(db.String(10), nullable=False)  # '入室' / '退出' など
    学科ID   = db.Column(db.SmallInteger, nullable=False)
    出席状態 = db.Column(db.Text)
    退出区分 = db.Column(db.Text)
    # 外部キーは敢えて貼らず、取り回し重視


class カメラログ(db.Model):
    __tablename__ = 'カメラログ'
    id       = db.Column(db.Integer, primary_key=True, autoincrement=True)
    記録時刻  = db.Column(db.Text, nullable=False)
    ソース    = db.Column(db.Text)
    ステータス = db.Column(db.Text)
    マーカー名 = db.Column(db.Text)
    スコア    = db.Column(db.Float)
    メッセージ = db.Column(db.Text)


class 入退室_入力(db.Model):
    __tablename__ = '入退室_入力'
    記録ID   = db.Column(db.Integer, primary_key=True, autoincrement=True)
    学生番号 = db.Column(db.Integer)
    生徒名   = db.Column(db.String(32))
    学科ID   = db.Column(db.SmallInteger, db.ForeignKey('学科.学科ID'))
    入退出時間 = db.Column(db.DateTime(timezone=True))
    入室区分 = db.Column(db.String)
    学科 = db.relationship('学科', backref=db.backref('入退室_入力_list', lazy=True))


class 授業計画(db.Model):
    __tablename__ = '授業計画'
    日付     = db.Column(db.Date, primary_key=True)
    期      = db.Column(db.SmallInteger, db.ForeignKey('期マスタ.期ID'))
    授業曜日  = db.Column(db.SmallInteger, db.ForeignKey('曜日マスタ.曜日ID'))
    備考     = db.Column(db.String(50))
    期マスタ  = db.relationship('期マスタ', backref=db.backref('授業計画_list', lazy=True))
    曜日マスタ = db.relationship('曜日マスタ', backref=db.backref('授業計画_list', lazy=True))


class 特別時間割(db.Model):
    __tablename__ = '特別時間割'
    日付   = db.Column(db.String, primary_key=True)  # 必要に応じて Date に変更可
    学科ID = db.Column(db.SmallInteger, db.ForeignKey('学科.学科ID'), primary_key=True)
    時限   = db.Column(db.SmallInteger, db.ForeignKey('TimeTable.時限'), primary_key=True)
    科目ID = db.Column(db.SmallInteger, db.ForeignKey('授業科目.授業科目ID'))
    教室ID = db.Column(db.SmallInteger, db.ForeignKey('教室.教室ID'))
    備考   = db.Column(db.String(50))
    学科     = db.relationship('学科', backref=db.backref('特別時間割_list', lazy=True))
    TimeTable = db.relationship('TimeTable', backref=db.backref('特別時間割_list', lazy=True))
    授業科目   = db.relationship('授業科目', backref=db.backref('特別時間割_list', lazy=True))
    教室     = db.relationship('教室', backref=db.backref('特別時間割_list', lazy=True))


class 欠席理由(db.Model):
    __tablename__ = '欠席理由'
    id       = db.Column(db.Integer, primary_key=True, autoincrement=True)
    学生番号   = db.Column(db.Integer, nullable=False)
    学科ID    = db.Column(db.SmallInteger, db.ForeignKey('学科.学科ID'), nullable=False)
    科目ID    = db.Column(db.SmallInteger, db.ForeignKey('授業科目.授業科目ID'), nullable=False)
    日付      = db.Column(db.Date, nullable=False)
    理由区分   = db.Column(db.Text, nullable=False)
    その他理由  = db.Column(db.Text)
    登録時刻   = db.Column(db.DateTime(timezone=True), server_default=func.now())
    学科     = db.relationship('学科', backref=db.backref('欠席理由_list', lazy=True))
    授業科目   = db.relationship('授業科目', backref=db.backref('欠席理由_list', lazy=True))

def _insert_initial_data():
    """データベースにマスタデータと初期データを挿入します。"""
    try:
        # TimeTable（時限マスタ）を挿入
        db.session.add_all([
            TimeTable(時限=1, 開始時刻=time(8, 50), 終了時刻=time(10, 30), 備考="1限目"),
            TimeTable(時限=2, 開始時刻=time(10, 35), 終了時刻=time(12, 15), 備考="2限目"),
            TimeTable(時限=3, 開始時刻=time(13, 0), 終了時刻=time(14, 40), 備考="3限目"),
            TimeTable(時限=4, 開始時刻=time(14, 45), 終了時刻=time(16, 25), 備考="4限目"),
            TimeTable(時限=5, 開始時刻=time(16, 40), 終了時刻=time(18, 20), 備考="5限目")
        ])

        # 入退室_入力（仮データ）挿入
        db.session.add_all([
            入退室_入力(記録ID=1, 学生番号=1, 生徒名='青井渓一郎', 学科ID=1, 入退出時間=datetime(2025, 4, 8, 8, 50), 入室区分='入室'),
            入退室_入力(記録ID=2, 学生番号=2, 生徒名='赤坂龍成', 学科ID=1, 入退出時間=datetime(2025, 4, 9, 8, 50), 入室区分='入室'),
            # 必要に応じて追加
        ])

        # 授業計画の挿入
        授業計画データ = [
            ('2025-04-08', 1, 2), ('2025-04-09', 1, 3), ('2025-04-10', 1, 4),
            ('2025-04-11', 1, 5), ('2025-04-14', 1, 1), ('2025-04-15', 1, 2),
            ('2025-04-16', 1, 3), ('2025-04-17', 1, 4), ('2025-04-18', 1, 5),
            ('2025-04-21', 1, 1), ('2025-04-22', 1, 2), ('2025-04-23', 1, 3),
            ('2025-04-24', 1, 4), ('2025-04-25', 1, 5), ('2025-04-28', 1, 1),
            ('2025-05-07', 1, 3), ('2025-05-08', 1, 4), ('2025-05-09', 1, 5),
            ('2025-05-12', 1, 1), ('2025-05-13', 1, 2), ('2025-05-15', 1, 4),
            ('2025-05-16', 1, 5), ('2025-05-19', 1, 1), ('2025-05-20', 1, 2),
            ('2025-05-21', 1, 3), ('2025-05-22', 1, 4), ('2025-05-23', 1, 5),
            ('2025-05-26', 1, 1), ('2025-05-27', 1, 2), ('2025-05-28', 1, 3),
            ('2025-05-29', 1, 4), ('2025-05-30', 1, 5), ('2025-06-02', 1, 1),
            ('2025-06-03', 1, 2), ('2025-06-04', 1, 3), ('2025-06-05', 1, 4),
            ('2025-06-06', 1, 5), ('2025-06-09', 1, 1), ('2025-06-10', 1, 2),
            ('2025-06-11', 1, 3), ('2025-06-12', 1, 4), ('2025-06-13', 1, 5),
            ('2025-06-16', 1, 1), ('2025-06-17', 1, 2), ('2025-06-18', 1, 3),
            ('2025-06-19', 2, 4), ('2025-06-20', 2, 5), ('2025-06-23', 2, 1),
            ('2025-06-24', 2, 2), ('2025-06-25', 2, 3), ('2025-06-26', 2, 4),
            ('2025-06-27', 2, 5), ('2025-06-30', 2, 1), ('2025-07-01', 2, 2),
            ('2025-07-02', 2, 3), ('2025-07-03', 2, 4), ('2025-07-04', 2, 5),
            ('2025-07-07', 2, 1), ('2025-07-08', 2, 2), ('2025-07-09', 2, 3),
            ('2025-07-10', 2, 4), ('2025-07-11', 2, 5), ('2025-07-14', 2, 1),
            ('2025-07-15', 9, 0), ('2025-07-16', 9, 0), ('2025-07-17', 9, 0),
            ('2025-07-18', 9, 0), ('2025-07-21', 9, 0), ('2025-07-22', 9, 0),
            ('2025-07-23', 9, 0), ('2025-07-24', 9, 0), ('2025-07-25', 9, 0),
            ('2025-08-20', 2, 3), ('2025-08-21', 2, 4), ('2025-08-22', 2, 5),
            ('2025-08-23', 2, 2), ('2025-08-25', 2, 1), ('2025-08-26', 2, 2),
            ('2025-08-27', 2, 3), ('2025-08-28', 2, 4), ('2025-08-29', 2, 5),
            ('2025-09-01', 2, 1), ('2025-09-02', 2, 2), ('2025-09-03', 2, 3),
            ('2025-09-04', 2, 4), ('2025-09-05', 2, 5), ('2025-09-08', 2, 1),
            ('2025-09-09', 2, 2), ('2025-09-10', 2, 3), ('2025-09-11', 2, 4),
            ('2025-09-12', 2, 5), ('2025-09-16', 2, 2), ('2025-09-17', 2, 3),
            ('2025-09-18', 2, 1), ('2025-09-19', 2, 5), ('2025-09-22', 2, 1),
            ('2025-09-24', 2, 3), ('2025-09-25', 2, 4), ('2025-09-26', 2, 2),
            ('2025-09-29', 2, 0), ('2025-09-30', 10, 0), ('2025-10-01', 10, 0),
            ('2025-10-02', 10, 0), ('2025-10-03', 10, 0), ('2025-10-06', 10, 0),
            ('2025-10-07', 10, 0), ('2025-10-08', 10, 0), ('2025-10-09', 10, 0),
            ('2025-10-10', 10, 0), ('2025-10-14', 3, 2), ('2025-10-15', 3, 3),
            ('2025-10-16', 3, 4), ('2025-10-17', 3, 5), ('2025-10-20', 3, 1),
            ('2025-10-21', 3, 2), ('2025-10-22', 3, 3), ('2025-10-23', 3, 4),
            ('2025-10-24', 3, 5), ('2025-10-27', 3, 1), ('2025-10-28', 3, 2),
            ('2025-10-29', 3, 3), ('2025-10-30', 3, 4), ('2025-10-31', 3, 5),
            ('2025-11-04', 3, 2), ('2025-11-05', 3, 3), ('2025-11-06', 3, 1),
            ('2025-11-07', 3, 5), ('2025-11-10', 3, 1), ('2025-11-11', 3, 2),
            ('2025-11-12', 3, 3), ('2025-11-13', 3, 4), ('2025-11-14', 3, 5),
            ('2025-11-17', 3, 1), ('2025-11-18', 3, 2), ('2025-11-19', 3, 3),
            ('2025-11-20', 3, 4), ('2025-11-21', 3, 5), ('2025-11-25', 3, 1),
            ('2025-11-26', 3, 3), ('2025-11-27', 3, 4), ('2025-11-28', 3, 5),
            ('2025-12-01', 3, 1), ('2025-12-02', 3, 2), ('2025-12-03', 3, 3),
            ('2025-12-04', 3, 4), ('2025-12-08', 3, 1), ('2025-12-09', 3, 2),
            ('2025-12-10', 3, 3), ('2025-12-11', 3, 4), ('2025-12-12', 3, 5),
            ('2025-12-15', 3, 1), ('2025-12-16', 3, 2), ('2025-12-18', 3, 4),
            ('2025-12-19', 3, 5), ('2025-12-17', 4, 3), ('2025-12-22', 4, 1),
            ('2025-12-23', 4, 2), ('2025-12-24', 4, 3), ('2025-12-25', 4, 4),
            ('2025-12-26', 4, 5), ('2026-01-13', 4, 1), ('2026-01-14', 4, 3),
            ('2026-01-15', 4, 4), ('2026-01-16', 4, 5), ('2026-01-19', 4, 1),
            ('2026-01-20', 4, 2), ('2026-01-21', 4, 3), ('2026-01-22', 4, 4),
            ('2026-01-23', 4, 5), ('2026-01-26', 4, 1), ('2026-01-27', 4, 2),
            ('2026-01-28', 4, 3), ('2026-01-29', 4, 4), ('2026-01-30', 4, 5),
            ('2026-02-02', 4, 1), ('2026-02-03', 4, 2), ('2026-02-04', 4, 3),
            ('2026-02-06', 4, 5), ('2026-02-09', 4, 1), ('2026-02-10', 4, 2),
            ('2026-02-12', 4, 4), ('2026-02-13', 4, 5), ('2026-02-16', 4, 1),
            ('2026-02-17', 4, 2), ('2026-02-18', 4, 3), ('2026-02-19', 4, 4),
            ('2026-02-20', 4, 5), ('2026-02-21', 4, 4), ('2026-02-24', 4, 2),
            ('2026-02-25', 4, 3), ('2026-02-26', 4, 4), ('2026-02-27', 4, 5),
            ('2026-03-02', 4, 1), ('2026-03-03', 4, 2), ('2026-03-04', 4, 3),
            ('2026-03-05', 4, 4), ('2026-03-06', 4, 5), ('2026-03-09', 4, 1),
            ('2026-03-10', 4, 2), ('2026-03-11', 4, 0)
        ]
        db.session.add_all([
            授業計画(日付=datetime.strptime(date_str, '%Y-%m-%d').date(), 期=期, 授業曜日=曜日)
            for date_str, 期, 曜日 in 授業計画データ
        ])

        # 期マスタを挿入
        db.session.add_all([
            期マスタ(期ID=1, 期名='Ⅰ'),
            期マスタ(期ID=2, 期名='Ⅱ'),
            期マスタ(期ID=3, 期名='Ⅲ'),
            期マスタ(期ID=4, 期名='Ⅳ'),
            期マスタ(期ID=5, 期名='Ⅴ'),
            期マスタ(期ID=6, 期名='Ⅵ'),
            期マスタ(期ID=7, 期名='Ⅶ'),
            期マスタ(期ID=8, 期名='Ⅷ'),
            期マスタ(期ID=9, 期名='前期(Ⅱ期)集中'),
            期マスタ(期ID=10, 期名='後期(Ⅲ期)集中'),
        ])

        # カメラログの挿入（仮データ）
        db.session.add_all([
            カメラログ(id=1, 記録時刻='2025-04-08 08:50:00', ソース='カメラ1', ステータス='正常', マーカー名='青井', スコア=0.95, メッセージ=''),
            # 必要に応じて追加
        ])

        # 学科を挿入
        db.session.add_all([
            学科(学科ID=1, 学科名='生産機械システム技術科'),
            学科(学科ID=2, 学科名='生産電気システム技術科'),
            学科(学科ID=3, 学科名='生産電子情報システム技術科'),
        ])

        # 教室を挿入
        db.session.add_all([
            教室(教室ID=1205, 教室名='A205', 収容人数=20),
            教室(教室ID=2102, 教室名='B102/103', 収容人数=20),
            教室(教室ID=2201, 教室名='B201', 収容人数=20),
            教室(教室ID=2202, 教室名='B202', 収容人数=20),
            教室(教室ID=2204, 教室名='B204', 収容人数=20),
            教室(教室ID=2205, 教室名='B205', 収容人数=20),
            教室(教室ID=2301, 教室名='B301', 収容人数=20),
            教室(教室ID=2302, 教室名='B302', 収容人数=20),
            教室(教室ID=2303, 教室名='B303', 収容人数=20),
            教室(教室ID=2304, 教室名='B304', 収容人数=20),
            教室(教室ID=2305, 教室名='B305', 収容人数=20),
            教室(教室ID=2306, 教室名='B306(視聴覚室)', 収容人数=20),
            教室(教室ID=3101, 教室名='C101(生産ロボット室)', 収容人数=20),
            教室(教室ID=3103, 教室名='C103(開発課題実習室)', 収容人数=20),
            教室(教室ID=3201, 教室名='C201', 収容人数=20),
            教室(教室ID=3202, 教室名='C202(応用課程計測制御応用実習室)', 収容人数=20),
            教室(教室ID=3203, 教室名='C203', 収容人数=20),
            教室(教室ID=3204, 教室名='C204', 収容人数=20),
            教室(教室ID=3231, 教室名='C231(資料室)', 収容人数=20),
            教室(教室ID=3301, 教室名='C301(マルチメディア実習室)', 収容人数=20),
            教室(教室ID=3302, 教室名='C302(システム開発実習室)', 収容人数=20),
            教室(教室ID=3303, 教室名='C303(システム開発実習室Ⅱ)', 収容人数=20),
            教室(教室ID=3304, 教室名='C304/305(応用課程生産管理ネットワーク応用実習室)', 収容人数=20),
            教室(教室ID=3306, 教室名='C306(共通実習室)', 収容人数=20),
            教室(教室ID=4102, 教室名='D102(回路基板加工室)', 収容人数=20),
            教室(教室ID=4201, 教室名='D201(開発課題実習室)', 収容人数=20),
            教室(教室ID=4202, 教室名='D202(電子情報技術科教官室)', 収容人数=20),
            教室(教室ID=4231, 教室名='D231(準備室)', 収容人数=20),
            教室(教室ID=4301, 教室名='D301', 収容人数=20),
            教室(教室ID=4302, 教室名='D302(PC実習室)', 収容人数=20),
        ])

        # 生徒を挿入
        db.session.add_all([
            生徒(学科ID=1, 学生番号=1, 生徒名='青井渓一郎'),
            生徒(学科ID=1, 学生番号=2, 生徒名='赤坂龍成'),
            生徒(学科ID=1, 学生番号=3, 生徒名='秋好拓海'),
            生徒(学科ID=1, 学生番号=4, 生徒名='伊川翔'),
            生徒(学科ID=1, 学生番号=5, 生徒名='岩切亮太'),
            生徒(学科ID=1, 学生番号=6, 生徒名='上田和輝'),
            生徒(学科ID=1, 学生番号=7, 生徒名='江本龍之介'),
            生徒(学科ID=1, 学生番号=8, 生徒名='大久保碧瀧'),
            生徒(学科ID=1, 学生番号=9, 生徒名='加來涼雅'),
            生徒(学科ID=1, 学生番号=10, 生徒名='梶原悠平'),
            生徒(学科ID=1, 学生番号=11, 生徒名='管野友富紀'),
            生徒(学科ID=1, 学生番号=12, 生徒名='髙口翔真'),
            生徒(学科ID=1, 学生番号=13, 生徒名='古城静雅'),
            生徒(学科ID=1, 学生番号=14, 生徒名='小柳知也'),
            生徒(学科ID=1, 学生番号=15, 生徒名='酒元翼'),
            生徒(学科ID=1, 学生番号=16, 生徒名='光寺孝彦'),
            生徒(学科ID=1, 学生番号=17, 生徒名='佐野勇太'),
            生徒(学科ID=1, 学生番号=18, 生徒名='清水健心'),
            生徒(学科ID=1, 学生番号=19, 生徒名='新谷雄飛'),
            生徒(学科ID=1, 学生番号=20, 生徒名='関原響樹'),
            生徒(学科ID=1, 学生番号=21, 生徒名='髙橋優人'),
            生徒(学科ID=1, 学生番号=22, 生徒名='武富義樹'),
            生徒(学科ID=1, 学生番号=23, 生徒名='内藤俊介'),
            生徒(学科ID=1, 学生番号=24, 生徒名='野田千尋'),
            生徒(学科ID=1, 学生番号=25, 生徒名='野中雄学'),
            生徒(学科ID=1, 学生番号=26, 生徒名='東奈月'),
            生徒(学科ID=1, 学生番号=27, 生徒名='古田雅也'),
            生徒(学科ID=1, 学生番号=28, 生徒名='牧野倭大'),
            生徒(学科ID=1, 学生番号=29, 生徒名='松隈駿介'),
            生徒(学科ID=1, 学生番号=30, 生徒名='宮岡嘉熙'),
            生徒(学科ID=3, 学生番号=1, 生徒名='青井渓一郎'),
            生徒(学科ID=3, 学生番号=2, 生徒名='赤坂龍成'),
            生徒(学科ID=3, 学生番号=3, 生徒名='秋好拓海'),
            生徒(学科ID=3, 学生番号=4, 生徒名='伊川翔'),
            生徒(学科ID=3, 学生番号=5, 生徒名='岩切亮太'),
            生徒(学科ID=3, 学生番号=6, 生徒名='上田和輝'),
            生徒(学科ID=3, 学生番号=7, 生徒名='江本龍之介'),
            生徒(学科ID=3, 学生番号=8, 生徒名='大久保碧瀧'),
            生徒(学科ID=3, 学生番号=9, 生徒名='加來涼雅'),
            生徒(学科ID=3, 学生番号=10, 生徒名='梶原悠平'),
            生徒(学科ID=3, 学生番号=11, 生徒名='管野友富紀'),
            生徒(学科ID=3, 学生番号=12, 生徒名='髙口翔真'),
            生徒(学科ID=3, 学生番号=13, 生徒名='古城静雅'),
            生徒(学科ID=3, 学生番号=14, 生徒名='小柳知也'),
            生徒(学科ID=3, 学生番号=15, 生徒名='酒元翼'),
            生徒(学科ID=3, 学生番号=16, 生徒名='光寺孝彦'),
            生徒(学科ID=3, 学生番号=17, 生徒名='佐野勇太'),
            生徒(学科ID=3, 学生番号=18, 生徒名='清水健心'),
            生徒(学科ID=3, 学生番号=19, 生徒名='新谷雄飛'),
            生徒(学科ID=3, 学生番号=20, 生徒名='関原響樹'),
            生徒(学科ID=3, 学生番号=21, 生徒名='髙橋優人'),
            生徒(学科ID=3, 学生番号=22, 生徒名='武富義樹'),
            生徒(学科ID=3, 学生番号=23, 生徒名='内藤俊介'),
            生徒(学科ID=3, 学生番号=24, 生徒名='野田千尋'),
            生徒(学科ID=3, 学生番号=25, 生徒名='野中雄学'),
            生徒(学科ID=3, 学生番号=26, 生徒名='東奈月'),
            生徒(学科ID=3, 学生番号=27, 生徒名='古田雅也'),
            生徒(学科ID=3, 学生番号=28, 生徒名='牧野倭大'),
            生徒(学科ID=3, 学生番号=29, 生徒名='松隈駿介'),
            生徒(学科ID=3, 学生番号=30, 生徒名='宮岡嘉熙'),
        ])

        # 授業科目を挿入
        db.session.add_all([
            授業科目(授業科目ID=301, 授業科目名='工業技術英語', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=302, 授業科目名='生産管理', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=303, 授業科目名='品質管理', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=304, 授業科目名='経営管理', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=305, 授業科目名='創造的開発技法', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=306, 授業科目名='工業法規', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=307, 授業科目名='職業能力開発体系論', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=308, 授業科目名='機械工学概論', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=309, 授業科目名='アナログ回路応用設計技術', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=310, 授業科目名='ディジタル回路応用設計技術', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=311, 授業科目名='複合電子回路応用設計技術', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=312, 授業科目名='ロボット工学', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=313, 授業科目名='通信プロトコル実装設計', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=314, 授業科目名='セキュアシステム設計', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=315, 授業科目名='組込システム設計', 学科ID=3, 単位=4, 学科フラグ=0),
            授業科目(授業科目ID=316, 授業科目名='安全衛生管理', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=317, 授業科目名='機械工作・組立実習', 学科ID=3, 単位=4, 学科フラグ=0),
            授業科目(授業科目ID=318, 授業科目名='実装設計製作実習', 学科ID=3, 単位=4, 学科フラグ=0),
            授業科目(授業科目ID=319, 授業科目名='EMC応用実習', 学科ID=3, 単位=4, 学科フラグ=0),
            授業科目(授業科目ID=320, 授業科目名='電子回路設計製作応用実習', 学科ID=3, 単位=4, 学科フラグ=0),
            授業科目(授業科目ID=321, 授業科目名='制御回路設計製作実習', 学科ID=3, 単位=4, 学科フラグ=0),
            授業科目(授業科目ID=322, 授業科目名='センシングシステム構築実習', 学科ID=3, 単位=4, 学科フラグ=0),
            授業科目(授業科目ID=323, 授業科目名='ロボット工学実習', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=324, 授業科目名='通信プロトコル実装実習', 学科ID=3, 単位=4, 学科フラグ=0),
            授業科目(授業科目ID=325, 授業科目名='セキュアシステム構築実習', 学科ID=3, 単位=4, 学科フラグ=0),
            授業科目(授業科目ID=326, 授業科目名='生産管理システム構築実習Ⅰ', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=327, 授業科目名='生産管理システム構築実習Ⅱ', 学科ID=3, 単位=2, 学科フラグ=0),
            授業科目(授業科目ID=328, 授業科目名='組込システム構築実習', 学科ID=3, 単位=4, 学科フラグ=0),
            授業科目(授業科目ID=329, 授業科目名='組込デバイス設計実習', 学科ID=3, 単位=4, 学科フラグ=0),
            授業科目(授業科目ID=330, 授業科目名='組込システム構築課題実習', 学科ID=3, 単位=10, 学科フラグ=0),
            授業科目(授業科目ID=331, 授業科目名='電子通信機器設計制作課題実習', 学科ID=3, 単位=10, 学科フラグ=0),
            授業科目(授業科目ID=332, 授業科目名='ロボット機器制作課題実習(電子情報)', 学科ID=3, 単位=10, 学科フラグ=0),
            授業科目(授業科目ID=333, 授業科目名='ロボット機器運用課題実習(電子情報)', 学科ID=3, 単位=10, 学科フラグ=0),
            授業科目(授業科目ID=380, 授業科目名='標準課題Ⅰ', 学科ID=3, 単位=10, 学科フラグ=0),
            授業科目(授業科目ID=381, 授業科目名='標準課題Ⅱ', 学科ID=3, 単位=10, 学科フラグ=0),
            授業科目(授業科目ID=334, 授業科目名='電子装置設計製作応用課題実習', 学科ID=3, 単位=54, 学科フラグ=0),
            授業科目(授業科目ID=335, 授業科目名='組込システム応用課題実習', 学科ID=3, 単位=54, 学科フラグ=0),
            授業科目(授業科目ID=336, 授業科目名='通信システム応用課題実習', 学科ID=3, 単位=54, 学科フラグ=0),
            授業科目(授業科目ID=337, 授業科目名='ロボットシステム応用課題実習', 学科ID=3, 単位=54, 学科フラグ=0),
            授業科目(授業科目ID=390, 授業科目名='開発課題', 学科ID=3, 単位=54, 学科フラグ=0)
        ])

        # 曜日マスタを挿入
        db.session.add_all([
            曜日マスタ(曜日ID=0, 曜日名='授業日'),
            曜日マスタ(曜日ID=1, 曜日名='月曜日'),
            曜日マスタ(曜日ID=2, 曜日名='火曜日'),
            曜日マスタ(曜日ID=3, 曜日名='水曜日'),
            曜日マスタ(曜日ID=4, 曜日名='木曜日'),
            曜日マスタ(曜日ID=5, 曜日名='金曜日'),
            曜日マスタ(曜日ID=6, 曜日名='土曜日'),
            曜日マスタ(曜日ID=7, 曜日名='日曜日'),
            曜日マスタ(曜日ID=8, 曜日名='祝祭日'),
        ])

        # 週時間割を挿入
        db.session.add_all([
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=1, 時限=1, 科目ID=325, 教室ID=3301, 備考='C304/寺内'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=1, 時限=2, 科目ID=325, 教室ID=3301, 備考='C304/寺内'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=1, 時限=3, 科目ID=301, 教室ID=2201, 備考='/ワット'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=1, 時限=4, 科目ID=313, 教室ID=3301, 備考='C302/中山'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=2, 時限=1, 科目ID=314, 教室ID=3301, 備考='C304/寺内'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=2, 時限=2, 科目ID=309, 教室ID=3301, 備考='C304/諏訪原'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=2, 時限=3, 科目ID=310, 教室ID=3301, 備考='/岡田'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=2, 時限=4, 科目ID=311, 教室ID=3301, 備考='C302/近藤'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=3, 時限=1, 科目ID=312, 教室ID=2301, 備考='B102/玉井'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=3, 時限=2, 科目ID=312, 教室ID=2301, 備考='B102/玉井'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=4, 時限=1, 科目ID=315, 教室ID=3302, 備考='/下泉'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=4, 時限=2, 科目ID=328, 教室ID=3302, 備考='/下泉'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=4, 時限=3, 科目ID=322, 教室ID=3302, 備考='/寺内'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=4, 時限=4, 科目ID=322, 教室ID=3302, 備考='/寺内'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=5, 時限=1, 科目ID=315, 教室ID=3302, 備考='/下泉'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=5, 時限=2, 科目ID=328, 教室ID=3302, 備考='/下泉'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=5, 時限=3, 科目ID=318, 教室ID=3302, 備考='/近藤'),
            週時間割(年度=2025, 学科ID=3, 期=1, 曜日=5, 時限=4, 科目ID=318, 教室ID=3302, 備考='/近藤'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=1, 時限=1, 科目ID=325, 教室ID=3301, 備考='/寺内'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=1, 時限=2, 科目ID=325, 教室ID=3301, 備考='/寺内'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=1, 時限=3, 科目ID=301, 教室ID=2201, 備考='/ワット'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=1, 時限=4, 科目ID=313, 教室ID=3301, 備考='/中山'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=2, 時限=1, 科目ID=325, 教室ID=3301, 備考='/寺内'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=2, 時限=2, 科目ID=309, 教室ID=3301, 備考='/諏訪原'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=2, 時限=3, 科目ID=310, 教室ID=3301, 備考='/岡田'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=2, 時限=4, 科目ID=311, 教室ID=3302, 備考='/近藤'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=3, 時限=1, 科目ID=324, 教室ID=3301, 備考='/中山'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=3, 時限=2, 科目ID=324, 教室ID=3301, 備考='/中山'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=4, 時限=1, 科目ID=323, 教室ID=3101, 備考='/電気系'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=4, 時限=2, 科目ID=323, 教室ID=3101, 備考='/電気系'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=4, 時限=3, 科目ID=315, 教室ID=3302, 備考='/下泉'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=4, 時限=4, 科目ID=328, 教室ID=3302, 備考='/下泉'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=5, 時限=3, 科目ID=322, 教室ID=3302, 備考='/玉井'),
            週時間割(年度=2025, 学科ID=3, 期=2, 曜日=5, 時限=4, 科目ID=322, 教室ID=3302, 備考='/玉井'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=1, 時限=1, 科目ID=327, 教室ID=3301, 備考='/中山'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=1, 時限=2, 科目ID=327, 教室ID=3301, 備考='/中山'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=1, 時限=3, 科目ID=380, 教室ID=3301, 備考='C302/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=1, 時限=4, 科目ID=380, 教室ID=3301, 備考='C302/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=2, 時限=1, 科目ID=317, 教室ID=3302, 備考='K302/機械系'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=2, 時限=2, 科目ID=317, 教室ID=3302, 備考='K302/機械系'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=2, 時限=3, 科目ID=380, 教室ID=3301, 備考='C302/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=2, 時限=4, 科目ID=380, 教室ID=3301, 備考='C302/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=3, 時限=1, 科目ID=329, 教室ID=3301, 備考='/岡田'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=3, 時限=2, 科目ID=329, 教室ID=3301, 備考='/岡田'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=3, 時限=3, 科目ID=308, 教室ID=2301, 備考='/上野'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=3, 時限=4, 科目ID=380, 教室ID=3301, 備考='C302/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=3, 時限=5, 科目ID=321, 教室ID=3302, 備考='/玉井'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=4, 時限=1, 科目ID=381, 教室ID=3302, 備考='C101/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=4, 時限=2, 科目ID=381, 教室ID=3302, 備考='C101/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=4, 時限=3, 科目ID=329, 教室ID=3301, 備考='/岡田'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=4, 時限=4, 科目ID=331, 教室ID=3302, 備考='C101/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=4, 時限=5, 科目ID=331, 教室ID=3302, 備考='C101/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=5, 時限=1, 科目ID=331, 教室ID=3302, 備考='C101/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=5, 時限=2, 科目ID=331, 教室ID=3302, 備考='C101/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=5, 時限=3, 科目ID=380, 教室ID=3301, 備考='C302/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=3, 曜日=5, 時限=4, 科目ID=380, 教室ID=3301, 備考='C302/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=4, 曜日=1, 時限=1, 科目ID=381, 教室ID=3302, 備考='C101/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=4, 曜日=1, 時限=2, 科目ID=381, 教室ID=3302, 備考='C101/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=4, 曜日=2, 時限=1, 科目ID=317, 教室ID=3302, 備考='K302/機械系'),
            週時間割(年度=2025, 学科ID=3, 期=4, 曜日=2, 時限=2, 科目ID=317, 教室ID=3302, 備考='K302/機械系'),
            週時間割(年度=2025, 学科ID=3, 期=4, 曜日=2, 時限=3, 科目ID=381, 教室ID=3302, 備考='C101/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=4, 曜日=2, 時限=4, 科目ID=381, 教室ID=3302, 備考='C101/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=4, 曜日=3, 時限=1, 科目ID=329, 教室ID=3301, 備考='/岡田'),
            週時間割(年度=2025, 学科ID=3, 期=4, 曜日=3, 時限=2, 科目ID=329, 教室ID=3301, 備考='/岡田'),
            週時間割(年度=2025, 学科ID=3, 期=4, 曜日=3, 時限=3, 科目ID=308, 教室ID=2301, 備考='/上野'),
            週時間割(年度=2025, 学科ID=3, 期=4, 曜日=4, 時限=1, 科目ID=331, 教室ID=3302, 備考='C101/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=4, 曜日=4, 時限=2, 科目ID=331, 教室ID=3302, 備考='C101/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=4, 曜日=4, 時限=3, 科目ID=331, 教室ID=3302, 備考='C101/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=4, 曜日=4, 時限=4, 科目ID=331, 教室ID=3302, 備考='C101/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=4, 曜日=5, 時限=1, 科目ID=331, 教室ID=3302, 備考='C101/電子情報系'),
            週時間割(年度=2025, 学科ID=3, 期=4, 曜日=5, 時限=2, 科目ID=331, 教室ID=3302, 備考='C101/電子情報系'),
        ])

        db.session.commit()
        print('マスタデータの挿入が完了しました。')
    except IntegrityError:
        # 既にデータが存在する場合 (UNIQUE制約違反など) はスキップ
        db.session.rollback()
        print('マスタデータは既に挿入されています。スキップしました。')
    except Exception as e:
        db.session.rollback()
        print(f"初期データ挿入中にエラーが発生しました: {e}")

# =========================================================================
# 初期化（初回のみ create_all）
# =========================================================================
def init_db_on_startup():
    """データベースの初期化を試行します。"""
    with app.app_context():
        try:
            # テーブルが存在するかを確認
            inspector = inspect(db.engine)
            if inspector.has_table('生徒'):
                print("[DB] 既存テーブルを検出。初期化スキップ。")
            else:
                print("[DB] 初回起動を検出。テーブル作成を開始します…")
                db.create_all()
                print("[DB] テーブル作成が完了しました。")

            # 初期データ挿入
            _insert_initial_data()  # 初期データの挿入関数をここで呼び出し

        except Exception as e:
            print(f"[DB] エラー: {e}")
# =========================================================================
# ルーティング（必要に応じて増やしてください）
# =========================================================================
def get_conn():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        app.logger.error(f"Database connection error: {e}")
        raise  # 再度エラーを投げて、エラーハンドリングを上位に任せる

def fetch_students():
    """List of students with gakka name."""
    with get_conn() as conn:
        # SQLAlchemyを使ってデータを取得する
        students = db.session.query(
            生徒.学科ID, 生徒.学生番号, 生徒.生徒名, 学科.学科名
        ).join(学科, 学科.学科ID == 生徒.学科ID).order_by(生徒.学科ID, 生徒.学生番号).all()
        return students

def fetch_recent_logs(limit=50):
    """Recent logs with limit."""
    with get_conn() as conn:
        # SQLAlchemyを使ってデータを取得
        logs = db.session.query(
            入退室.記録ID, 入退室.学生番号, 入退室.生徒名, 
            func.to_char(入退室.入退出時間, 'YYYY-MM-DD HH24:MI:SS.US').label('入退出時間'),  # PostgreSQLでの日付フォーマット
            入退室.入室区分, 入退室.出席状態, 入退室.学科ID, 学科.学科名
        ).join(学科, 学科.学科ID == 入退室.学科ID).order_by(
            入退室.入退出時間.desc(), 入退室.記録ID.desc()
        ).limit(limit).all()
        return logs

def fetch_gakkas():
    """List of gakkas."""
    with get_conn() as conn:
        # SQLAlchemyを使ってデータを取得
        gakkas = db.session.query(学科.学科ID, 学科.学科名).order_by(学科.学科ID).all()
        return gakkas

def fetch_recent_camlogs(limit=100):
    """Fetch recent cam logs."""
    ensure_camlog_table()  # カメラログテーブルが必要であれば作成する
    with get_conn() as conn:
        # SQLAlchemyを使ってデータを取得
        # web2.py (fetch_recent_camlogs 関数内)
        camlogs = db.session.query(
        カメラログ.id,
        カメラログ.記録時刻,
        カメラログ.ソース,
        カメラログ.ステータス,
        func.coalesce(カメラログ.マーカー名, '').label('マーカー名'),
        # 💥 ここが問題: NULLの場合に空文字列 '' を使っている
        func.coalesce(カメラログ.スコア, 0.0).label('スコア'), 
        func.coalesce(カメラログ.メッセージ, '').label('メッセージ')
    ).order_by(カメラログ.記録時刻.desc(), カメラログ.id.desc()).limit(limit).all()
        
        camlogs = db.session.query(
            # ... その他の列
            func.coalesce(カメラログ.マーカー名, '').label('マーカー名'),
            # ✅ NULLの場合は数値の 0.0 を返すように修正
            func.coalesce(カメラログ.スコア, 0.0).label('スコア'), 
            func.coalesce(カメラログ.メッセージ, '').label('メッセージ')
        ).order_by(カメラログ.記録時刻.desc(), カメラログ.id.desc()).limit(limit).all()
        return camlogs

def fetch_timetable_1to4():
    """Fetch 1 to 4 periods timetable."""
    with get_conn() as conn:
        # SQLAlchemyを使ってデータを取得
        timetable = db.session.query(
            TimeTable.時限, TimeTable.開始時刻, TimeTable.終了時刻
        ).filter(TimeTable.時限.between(1, 4)).order_by(TimeTable.時限).all()
        return timetable

def ensure_camlog_table():
    """Ensure the camera log table exists in PostgreSQL."""
    with app.app_context():
        # SQLAlchemy を使ってカメラログテーブルが存在するか確認
        if not inspect(db.engine).has_table('カメラログ'):
            # テーブルが存在しない場合、PostgreSQL 用にテーブルを作成する
            db.create_all()  # テーブルを作成する
            print("カメラログテーブルを作成しました。")

@app.route("/")
def index():
    # データを取得
    students = fetch_students()            # 生徒データ
    logs = fetch_recent_logs(limit=50)    # 入退室ログ
    gakkas = fetch_gakkas()               # 学科データ
    camlogs = fetch_recent_camlogs(limit=100)  # カメラログデータ
    tt_1to4 = fetch_timetable_1to4()      # 時限1～4のデータを取得
    # index.htmlテンプレートをレンダリング
    return render_template(
        "index.html",
        students=students,
        logs=logs,
        gakkas=gakkas,
        today=date.today().isoformat(),
        # ⚠️ ここにカンマがないため次の行がエラーになる
        db_path=DATABASE_URL, # DBのパス
        camlogs=camlogs,
        tt_1to4=tt_1to4
    )

def require_logs_auth(view_func):
    """ /logs 用の簡易パスワード認証 """
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        # セッションに 'logs_ok' がセットされていれば、認証済みと見なす
        if session.get("logs_ok"):
            return view_func(*args, **kwargs)
        # 未認証 → ログイン画面へリダイレクト。nextパラメータで元のURLを渡す。
        return redirect(url_for("logs_login", next=request.path))
    return wrapper

# 💡 新規追加: submit エンドポイント
@app.route("/submit", methods=["POST"])
def submit():
    try:
        # フォームデータから学生番号と学科IDを取得（intに変換）
        学生番号 = int(request.form.get("student_no"))
        学科ID = int(request.form.get("gakka_id"))

        # タイムスタンプを正規化
        # normalize_ts 関数は元のファイルに存在すると仮定します
        ts = normalize_ts(request.form.get("ts_local") or request.form.get("ts"))

        # 日時形式のチェック
        if (request.form.get("ts_local") or request.form.get("ts")) and not ts:
            flash("日時形式が不正です。datetime-local の値を確認してください。")
            return redirect(url_for("index"))

        # 生徒マスタに存在するかチェック
        # get_official_student 関数は元のファイルに存在すると仮定します
        official_name = get_official_student(学生番号, 学科ID)
        if not official_name:
            flash("生徒マスタに存在しません。先に『生徒』テーブルへ登録してください。")
            return redirect(url_for("index"))

        # 入退室の生データ（入力）を記録
        # insert_attendance_input 関数は元のファイルに存在すると仮定します
        insert_attendance_input(学生番号, official_name, 学科ID, ts)

        flash(f"学生番号:{学生番号} ({official_name}) の入退室を記録しました。")
    except Exception as e:
        # エラー処理
        flash(f"エラーが発生しました: {e}")

    return redirect(url_for("index"))

@app.route("/logs")
@require_logs_auth
def logs():
    # 認証済みの場合のみ実行される
    # fetch_recent_logs と fetch_recent_camlogs は他の場所で定義されている必要があります
    logs = fetch_recent_logs(limit=50)
    camlogs = fetch_recent_camlogs(limit=100)
    
    return render_template(
        "logs.html", 
        logs=logs, 
        camlogs=camlogs, 
        today=date.today().isoformat() # date.today() を使用するため、datetime モジュールも必要
    )

@app.route("/healthz")
def healthz():
    # Renderのヘルスチェックや動作確認用
    return jsonify(ok=True, db=type(db.engine.dialect).__name__)

# =========================================================================
# 起動
# =========================================================================

init_db_on_startup()

if __name__ == "__main__":
    # ローカル開発向け
    init_db_on_startup()
    port = int(os.environ.get("PORT", "5000"))
    print("\n-------------------------------------------")
    print("ORMベースのFlask Webアプリを起動します。")
    print("Render環境では Procfile: `web: gunicorn main:app` を使ってください。")
    app.run(debug=True, host="0.0.0.0", port=port)


















