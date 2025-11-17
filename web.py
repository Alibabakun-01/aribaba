# main.py (Flask-SQLAlchemy ORM 統合版 - Render対応/安定化)
import calendar
import csv
import psycopg2
import os
from typing import Optional, Any # <<< これを追加
from datetime import datetime, timedelta, time, date as date_cls, date
from flask import Flask, render_template, render_template_string, request, url_for, jsonify, redirect, flash, session, abort, send_file, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, text, inspect
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.exc import IntegrityError  # ここでインポート
from sqlalchemy.orm import aliased
from functools import wraps
from io import BytesIO, StringIO
from collections import defaultdict
from psycopg2.extras import RealDictCursor

# from .web import db, TimeTable, 学科, 授業科目, session # 仮に web.py から import されていると仮定

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
# 環境変数からパスワードを取得
LOGS_PASSWORD = os.environ.get("LOGS_PASSWORD", "kojou")

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
def default_month_range():
    """今月の1日〜今日を YYYY-MM-DD で返す"""
    # datetime モジュールのインポートが必要
    from datetime import date
    today = date.today()
    start = today.replace(day=1).isoformat()
    end = today.isoformat()
    return start, end

# =========================================================================
# 生徒/学科マスタ取得（ORM利用）
# =========================================================================

def get_official_student(学生番号: int, 学科ID: int) -> Optional[str]:
    """マスタテーブルから正式な生徒名を取得します（ORM版）。"""
    student = 生徒.query.filter(
        生徒.学生番号 == 学生番号,
        生徒.学科ID == 学科ID
    ).first()
    return student.生徒名 if student else None

# =========================================================================
# サマリー集計関数（ORM利用）
# =========================================================================

def fetch_attendance_totals(学生番号: int, 学科ID: int, start_date: str, end_date: str):
    """指定期間の出欠合計回数を集計します（ORM版）。"""
    # 出席状態ごとのカウントをDBで集計
    counts = db.session.query(
        入退室.出席状態,
        func.count(入退室.出席状態).label('cnt')
    ).filter(
        入退室.学生番号 == 学生番号,
        入退室.学科ID == 学科ID,
        入退室.入室区分 == '入室',
        # PostgreSQLのDATE型キャストと期間指定
        func.cast(入退室.入退出時間, Date) >= start_date,
        func.cast(入退室.入退出時間, Date) <= end_date,
        入退室.出席状態.in_(['出席', '遅刻', '欠席'])
    ).group_by(入退室.出席状態).all()

    totals = {"出席": 0, "遅刻": 0, "欠席": 0}
    for status, count in counts:
        # ORMの結果はタプルまたは属性アクセス
        totals[status] = count

    totals["合計"] = sum(totals.values())
    return totals

def export_csv_to_memory(start_date: Optional[str] = None, end_date: Optional[str] = None,
                          学生番号: Optional[int] = None, 学科ID: Optional[int] = None) -> BytesIO:
    where, params = [], []
    if start_date:
        where.append("DATE(i.入退出時間, 'localtime') >= ?")
        params.append(start_date)
    if end_date:
        where.append("DATE(i.入退出時間, 'localtime') <= ?")
        params.append(end_date)
    if 学生番号 is not None:
        where.append("i.学生番号 = ?")
        params.append(学生番号)
    if 学科ID is not None:
        where.append("i.学科ID = ?")
        params.append(学科ID)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    sql = f"""
        SELECT i.記録ID, i.学生番号, i.生徒名,
              strftime('%Y-%m-%d %H:%M:%f', i.入退出時間) AS 入退出時間,
              i.入室区分, i.学科ID, IFNULL(g.学科名,'') AS 学科名
        FROM 入退室 i
        LEFT JOIN 学科 g ON g.学科ID = i.学科ID
        {where_sql}
        ORDER BY i.入退出時間 ASC, i.記録ID ASC
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()

    text_stream = StringIO()
    writer = csv.writer(text_stream)
    headers = ["記録ID", "学生番号", "生徒名", "入退出時間", "入室区分", "学科ID", "学科名"]
    writer.writerow(headers)
    for r in rows:
        writer.writerow([r[h] for h in headers])

    data = text_stream.getvalue().encode("utf-8-sig")
    buf = BytesIO(data)
    buf.seek(0)
    return buf

def normalize_ts(ts_input: Optional[str]) -> Optional[str]:
    if not ts_input:
        return None
    s = ts_input.strip().replace('T', ' ')
    fmts = ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None

def get_attendance_status(入室時刻: str) -> str:
    try:
        dt = datetime.strptime(入室時刻, "%Y-%m-%d %H:%M:%S")
        rec = resolve_period_for(dt)
        t = dt.time()
        if rec:
            if t <= rec["start"]:
                return "出席"
            elif t <= rec["end"]:
                return "遅刻"
            else:
                return "欠席"
        # fallback（TimeTableなし）
        on_time = datetime.strptime(ATT_ON_TIME, "%H:%M:%S").time()
        absent_t = datetime.strptime(ATT_ABSENT, "%H:%M:%S").time()
        if t <= on_time:
            return "出席"
        elif t <= absent_t:
            return "遅刻"
        else:
            return "欠席"
    except Exception:
        return "不正な時刻"

def get_exit_attendance_status(退出時刻: str) -> str:
    try:
        dt = datetime.strptime(退出時刻, "%Y-%m-%d %H:%M:%S")
        rec = resolve_period_for(dt)
        if rec:
            return "一時退出" if dt.time() < rec["end"] else "退出"
        # fallback（TimeTableなし）
        absent_t = datetime.strptime(ATT_ABSENT, "%H:%M:%S").time()
        return "一時退出" if dt.time() < absent_t else "退出"
    except Exception:
        return "退出"

# ====== Last status ======
# def get_last_status(学生番号: int, 学科ID: int) -> Optional[str]:
#     with get_conn() as conn:
#         cur = conn.cursor()
#         cur.execute("""
#             SELECT "入室区分"
#             FROM "入退室"
#             WHERE "学生番号" = %s AND "学科ID" = %s
#             ORDER BY "入退出時間" DESC, "記録ID" DESC
#             LIMIT 1
#         """, (学生番号, 学科ID))
#         row = cur.fetchone()
#         return row["入室区分"] if row else None

def get_last_status(学生番号: int, 学科ID: int) -> Optional[str]:
    """
    指定された学生の直近の「入室区分」を返す。
    レコードが無ければ None を返す。
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT "入室区分"
            FROM "入退室"
            WHERE "学生番号" = %s AND "学科ID" = %s
            ORDER BY "入退出時間" DESC, "記録ID" DESC
            LIMIT 1
        """, (学生番号, 学科ID))
        row = cur.fetchone()
        return row["入室区分"] if row else None

def fetch_daily_first_checkin(学生番号: int, 学科ID: int, start_date: str, end_date: str):
    """期間内の各日の最初の入室ログを取得します（ORM/PostgreSQL版）。"""
    # PostgreSQLでは、ウィンドウ関数を使用して各日の最初のエントリを見つける
    
    # ウィンドウ関数で順位付けするサブクエリを作成
    subquery = db.session.query(
        入退室,
        func.row_number().over(
            # 日付ごとにパーティションし、入退出時間で昇順ソート
            partition_by=func.date(入退室.入退出時間),
            order_by=入退室.入退出時間.asc()
        ).label('rn')
    ).filter(
        入退室.学生番号 == 学生番号,
        入退室.学科ID == 学科ID,
        入退室.入室区分 == '入室',
        func.cast(入退室.入退出時間, Date) >= start_date,
        func.cast(入退室.入退出時間, Date) <= end_date
    ).subquery()

    # ランキングが1位（最初の入室）の行を選択
    FirstCheckin = aliased(入退室, subquery)
    
    results = db.session.query(
        func.date(FirstCheckin.入退出時間).label('日付'),
        FirstCheckin.入退出時間.label('最初入室'),
        FirstCheckin.出席状態
    ).filter(
        subquery.c.rn == 1
    ).order_by(
        func.date(FirstCheckin.入退出時間).desc()
    ).all()
    
    # 結果を辞書リストに変換 (Jinjaテンプレートへの引き渡しを想定)
    daily_list = [
        {"日付": r.日付, "最初入室": r.最初入室, "出席状態": r.出席状態}
        for r in results
    ]
    return daily_list

# ====== Common Utils ======
def normalize_ts(ts_input: Optional[str]) -> Optional[str]:
    if not ts_input:
        return None
    s = ts_input.strip().replace('T', ' ')
    fmts = ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None

def get_attendance_status(入室時刻: str) -> str:
    try:
        dt = datetime.strptime(入室時刻, "%Y-%m-%d %H:%M:%S")
        rec = resolve_period_for(dt)
        t = dt.time()
        if rec:
            if t <= rec["start"]:
                return "出席"
            elif t <= rec["end"]:
                return "遅刻"
            else:
                return "欠席"
        # fallback（TimeTableなし）
        on_time = datetime.strptime(ATT_ON_TIME, "%H:%M:%S").time()
        absent_t = datetime.strptime(ATT_ABSENT, "%H:%M:%S").time()
        if t <= on_time:
            return "出席"
        elif t <= absent_t:
            return "遅刻"
        else:
            return "欠席"
    except Exception:
        return "不正な時刻"

def get_exit_attendance_status(退出時刻: str) -> str:
    try:
        dt = datetime.strptime(退出時刻, "%Y-%m-%d %H:%M:%S")
        rec = resolve_period_for(dt)
        if rec:
            return "一時退出" if dt.time() < rec["end"] else "退出"
        # fallback（TimeTableなし）
        absent_t = datetime.strptime(ATT_ABSENT, "%H:%M:%S").time()
        return "一時退出" if dt.time() < absent_t else "退出"
    except Exception:
        return "退出"

# ====== Insert entry (existing logic kept) ======
# def insert_attendance_input(学生番号: int, 生徒名: str, 学科ID: int, 入退出時間: Optional[str] = None):
#     ts = normalize_ts(入退出時間) if 入退出時間 else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
#     last = get_last_status(学生番号, 学科ID)
#     next_status = "退出" if last == "入室" else "入室"

#     with get_conn() as conn:
#         cur = conn.cursor()

#         # Ensure column 出席状態 exists (do not auto-alter by default)
#         cur.execute("PRAGMA table_info(入退室);")
#         cols = [r[1] for r in cur.fetchall()]
#         if "出席状態" not in cols:
#             # 既存DBに合わせるため、ここでは自動追加しない
#             pass

#         # Decide attendance status
#         if next_status == "入室":
#             att = get_attendance_status(ts)
#         else:
#             att = get_exit_attendance_status(ts)

#         cur.execute("""
#             INSERT INTO 入退室 (学生番号, 生徒名, 学科ID, 入退出時間, 入室区分, 出席状態)
#             VALUES (?,?,?,?,?,?)
#         """, (学生番号, 生徒名, 学科ID, ts, next_status, att))
#         conn.commit()

def insert_attendance_input(学生番号: int, 生徒名: str, 学科ID: int,
                            入退出時間: Optional[str] = None):
    # タイムスタンプの決定（省略時は現在時刻）
    ts = normalize_ts(入退出時間) if 入退出時間 else datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 直前の入室区分から次の状態を決める
    last = get_last_status(学生番号, 学科ID)
    next_status = "退出" if last == "入室" else "入室"

    with get_conn() as conn:
        cur = conn.cursor()

        # 🔽 ここにあった PRAGMA / information_schema によるカラムチェックは削除しました
        # 「出席状態」カラムは事前に DB で作成しておく前提で動かします

        # 出席状態の判定
        if next_status == "入室":
            att = get_attendance_status(ts)
        else:
            att = get_exit_attendance_status(ts)

        # PostgreSQL 用 INSERT（%s ＋ ダブルクォート）
        cur.execute("""
            INSERT INTO "入退室"
              ("学生番号", "生徒名", "学科ID", "入退出時間", "入室区分", "出席状態")
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (学生番号, 生徒名, 学科ID, ts, next_status, att))

        conn.commit()

def ensure_absent_reason_table():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS 欠席理由 (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              学生番号 INTEGER NOT NULL,
              学科ID  SMALLINT NOT NULL,
              科目ID  SMALLINT NOT NULL,
              日付     DATE NOT NULL,
              理由区分 TEXT NOT NULL,     -- '病欠','公欠','寝坊','その他'
              その他理由 TEXT,
              登録時刻 DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now','localtime')),
              UNIQUE(学生番号, 学科ID, 科目ID, 日付)
            )
        """)
        conn.commit()

def fetch_absent_reasons_map(学生番号: int, 学科ID: int, 科目ID: int):
    """(日付 -> dict{理由区分, その他理由}) のマップを返す"""
    ensure_absent_reason_table()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT 日付, 理由区分, IFNULL(その他理由,'') AS その他理由
            FROM 欠席理由
            WHERE 学生番号= %s AND 学科ID= %s AND 科目ID= %s
        """, (学生番号, 学科ID, 科目ID))
        rows = cur.fetchall()
    return { r["日付"]: {"理由区分": r["理由区分"], "その他理由": r["その他理由"]} for r in rows }

def upsert_absent_reason(学生番号: int, 学科ID: int, 科目ID: int, 日付: str, 理由区分: str, その他理由: str = ""):
    ensure_absent_reason_table()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO 欠席理由(学生番号,学科ID,科目ID,日付,理由区分,その他理由)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(学生番号,学科ID,科目ID,日付)
            DO UPDATE SET 理由区分=excluded.理由区分, その他理由=excluded.その他理由,
                         登録時刻=(strftime('%Y-%m-%d %H:%M:%S','now','localtime'))
        """, (学生番号, 学科ID, 科目ID, 日付, 理由区分, その他理由))
        conn.commit()

# ====== Generate Monthly Schedule ======

def generate_monthly_schedule(selected_month=None, selected_year=None):
    ensure_special_schedule()

    with get_conn() as conn:
        cur = conn.cursor()
        # 週時間割・授業計画・科目/教室名を先読み
        cur.execute("""SELECT 年度, 学科ID, 期, 曜日, 時限, 科目ID, 教室ID, 備考 FROM 週時間割""")
        week_schedule = cur.fetchall()

        cur.execute("""SELECT 日付, 期, 授業曜日, 備考 FROM 授業計画""")
        class_schedule = cur.fetchall()

        cur.execute("""SELECT 授業科目ID, 授業科目名 FROM 授業科目""")
        subj_map = {r["授業科目ID"]: r["授業科目名"] for r in cur.fetchall()}

        cur.execute("""SELECT 教室ID, 教室名 FROM 教室""")
        room_map = {r["教室ID"]: r["教室名"] for r in cur.fetchall()}

    # 特別時間割（指定月だけを読み込む）
    special = {}
    if selected_month and selected_year:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
              SELECT 日付, 学科ID, 時限, 科目ID, 教室ID, 備考
              FROM 特別時間割
              WHERE strftime('%Y', 日付)= %s AND strftime('%m', 日付)= %s
            """, (str(selected_year), f"{selected_month:02d}"))
            for r in cur.fetchall():
                d = datetime.strptime(r["日付"], "%Y-%m-%d").date()
                key = (d, r["学科ID"], r["時限"])
                special[key] = dict(r)

    # 月ごとの時間割
    monthly_schedule = defaultdict(lambda: defaultdict(list))  # 月 -> 日 -> リスト

    for c in class_schedule:
        # 授業計画の日付を決定
        d = datetime.strptime(c["日付"], "%Y/%m/%d").date() if "/" in c["日付"] else datetime.strptime(c["日付"], "%Y-%m-%d").date()
        month = d.month
        day = d.day

        if selected_month and month != selected_month:
            continue
        if selected_year and d.year != selected_year:
            continue

        term = c["期"]
        youbi = c["授業曜日"]

        # 対象日の全学科×時限候補（週時間割から） 
        for w in week_schedule:
            if w["期"] == term and w["曜日"] == youbi:
                gakka_id = w["学科ID"]
                period = w["時限"]

                # 特別時間割で上書きがあればそれを使う
                sp = special.get((d, gakka_id, period))
                if sp:
                    subj_id = sp["科目ID"]
                    room_id = sp["教室ID"]
                    note = sp["備考"]
                else:
                    subj_id = w["科目ID"]
                    room_id = w["教室ID"]
                    note = w["備考"]

                subject_name = subj_map.get(subj_id, "（未設定）") if subj_id else "（空コマ）"
                room_name = room_map.get(room_id, "") if room_id else ""

                monthly_schedule[month][day].append({
                    "時限": period,
                    "学科ID": gakka_id,
                    "科目名": subject_name + (f"（{room_name}）" if room_name else ""),
                    "教室ID": room_id,
                    "備考": note or ""
                })

        # 同日の中で時限順に整列
        if monthly_schedule[month][day]:
            monthly_schedule[month][day].sort(key=lambda x: (x["時限"], x["学科ID"]))

    return monthly_schedule


# ====== Camera Log (new, minimal addition) ======
def ensure_special_schedule():
    """日付ごとの例外（上書き）時間割テーブル"""
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS 特別時間割 (
            日付      TEXT    NOT NULL,         -- 'YYYY-MM-DD'
            学科ID    SMALLINT NOT NULL,
            時限      TINYINT NOT NULL,
            科目ID    SMALLINT,                 -- NULL=空コマ
            教室ID    SMALLINT,
            備考      NVARCHAR(50),
            PRIMARY KEY(日付, 学科ID, 時限)
        )
        """)
        conn.commit()

def add_camlog(記録時刻: str, ソース: str, ステータス: str,
               マーカー名: str = None, スコア: float = None, メッセージ: str = None):
    ensure_special_schedule()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO カメラログ (記録時刻, ソース, ステータス, マーカー名, スコア, メッセージ)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (記録時刻, ソース, ステータス, マーカー名, スコア, メッセージ))
        conn.commit()

def fetch_daily_inout(学生番号: int, 学科ID: int, start_date: str, end_date: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            WITH first_in AS (
              SELECT
                DATE(入退出時間,'localtime') AS 日付,
                MIN(入退出時間) AS 最初入室時刻
              FROM 入退室
              WHERE 学生番号= %s AND 学科ID= %s AND 入室区分='入室'
                AND DATE(入退出時間,'localtime') BETWEEN ? AND ?
              GROUP BY DATE(入退出時間,'localtime')
            ),
            last_out AS (
              SELECT
                DATE(入退出時間,'localtime') AS 日付,
                MAX(入退出時間) AS 最後退出時刻
              FROM 入退室
              WHERE 学生番号= %s AND 学科ID= %s AND 入室区分='退出'
                AND DATE(入退出時間,'localtime') BETWEEN ? AND ?
              GROUP BY DATE(入退出時間,'localtime')
            ),
            days AS (
              SELECT 日付 FROM first_in
              UNION
              SELECT 日付 FROM last_out
            )
            SELECT
              d.日付,
              tin.入退出時間 AS 最初入室,
              tin.出席状態   AS 最初入室_出席状態,
              tout.入退出時間 AS 最後退出,
              tout.出席状態   AS 最後退出_出席状態
            FROM days d
            LEFT JOIN first_in fi ON fi.日付 = d.日付
            LEFT JOIN last_out lo ON lo.日付 = d.日付
            LEFT JOIN 入退室 tin
              ON fi.最初入室時刻 IS NOT NULL
             AND tin.入退出時間 = fi.最初入室時刻
             AND DATE(tin.入退出時間,'localtime') = d.日付
             AND tin.学生番号 = ?
             AND tin.学科ID   = ?
            LEFT JOIN 入退室 tout
              ON lo.最後退出時刻 IS NOT NULL
             AND tout.入退出時間 = lo.最後退出時刻
             AND DATE(tout.入退出時間,'localtime') = d.日付
             AND tout.学生番号 = ?
             AND tout.学科ID   = ?
            ORDER BY d.日付 DESC
        """, (
            学生番号, 学科ID, start_date, end_date,   # first_in
            学生番号, 学科ID, start_date, end_date,   # last_out
            学生番号, 学科ID,                          # join tin
            学生番号, 学科ID                           # join tout
        ))
        return cur.fetchall()

def fetch_attendance_details(学生番号: int, 学科ID: int, start_date: str, end_date: str):
    """期間内のすべての入退室ログの詳細を取得します（簡素化ORM版）。"""
    # 複雑なPythonロジックはテンプレート/フロントエンド側での処理を推奨するため、
    # ここではデータベースから必要なログをシンプルに取得します。

    results = 入退室.query.filter(
        入退室.学生番号 == 学生番号,
        入退室.学科ID == 学科ID,
        func.cast(入退室.入退出時間, Date) >= start_date,
        func.cast(入退室.入退出時間, Date) <= end_date
    ).order_by(入退室.入退出時間.asc()).all()

    # 必要な情報を含む辞書リストとして返す（詳細な計算ロジックは削除）
    details = [
        {"入退出時間": r.入退出時間, "入室区分": r.入室区分, "出席状態": r.出席状態}
        for r in results
    ]
    
    # **注意**: 元の複雑なロジック（resolve_period_for, timedelta計算など）は
    # この簡素化されたORM版には含まれていません。必要に応じて再実装が必要です。
    return details

def fetch_subject_attendance_rates(学生番号: int, 学科ID: int, start_date: str, end_date: str):
    """科目ごとの出席率を集計する処理（スタブ・未実装）"""
    # この関数は、元のコードが複数の複雑なマスタテーブル（授業計画、週時間割など）
    # への依存が強く、SQLAlchemy ORMでの完全な移植には全体のデータベーススキーマと
    # 複雑な結合ロジックの完全な再構築が必要です。
    # 実行時のクラッシュを防ぐため、ここではダミーデータを返します。
    return [
        {"授業科目": "科目A", "出席": 15, "遅刻": 1, "欠席": 4, "出席率(%)": "75.0"},
        {"授業科目": "科目B", "出席": 10, "遅刻": 0, "欠席": 1, "出席率(%)": "90.9"},
    ]

# def get_conn():
#     try:
#         conn = psycopg2.connect(DATABASE_URL)
#         return conn
#     except Exception as e:
#         app.logger.error(f"Database connection error: {e}")
#         raise  # 再度エラーを投げて、エラーハンドリングを上位に任せる
def get_conn():
    """
    PostgreSQL への接続。
    fetchone()/fetchall() の戻り値が dict になるので、
    row["入室区分"], row["学科ID"] などがそのまま使える。
    """
    try:
        conn = psycopg2.connect(
            DATABASE_URL,
            cursor_factory=RealDictCursor  # ← ★これが重要！
        )
        return conn
    except Exception as e:
        app.logger.error(f"Database connection error: {e}")
        raise


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

def column_exists(table_class, column: str) -> bool:
    """
    指定された SQLAlchemy ORM モデルクラス (テーブル) に指定されたカラムが存在するかチェックする。
    PostgreSQL環境では PRAGMA table_info は使えないため、SQLAlchemyの inspect を使用。
    """
    try:
        # モデルクラス（テーブル）からマッピング情報を取得
        insp = inspect(table_class)
        # カラム名がそのマッピング情報に含まれているかチェック
        return column in insp.columns
    except Exception as e:
        # モデルがまだマップされていない、などのエラーハンドリング
        print(f"Error inspecting table {table_class.__name__}: {e}")
        return False

def get_gakka_id_by_name(学科名: str) -> Optional[int]:
    """Resolve 学科名 -> 学科ID (ORM)."""
    gakka = db.session.query(学科.学科ID).filter(学科.学科名 == 学科名).first()
    return gakka[0] if gakka else None

def get_subject_name_by_id(subject_id: int) -> str:
    """授業科目IDから授業科目名を取得 (ORM)."""
    subject = db.session.query(授業科目.授業科目名).filter(授業科目.授業科目ID == subject_id).first()
    return subject[0] if subject else '未設定'

def _next_subject_id() -> int:
    """次に使用する授業科目IDを取得 (COALESCE(MAX(ID), 0) + 1) (ORM)."""
    # MAX(授業科目ID) を取得し、結果が None の場合は 0 を使用
    max_id = db.session.query(func.max(授業科目.授業科目ID)).scalar()
    return (max_id or 0) + 1

# =========================================================================
# TimeTable Utility
# =========================================================================

def _parse_int(value: Any, default: Optional[int]=None) -> Optional[int]:
    """文字列を整数に安全に変換する。"""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

def _parse_hhmm_or_hhmmss(s: str) -> time:
    """'8:50' / '08:50' / '08:50:00' を time に変換"""
    s = (s or "").strip()
    
    # タイムゾーン情報を持つ可能性のあるフォーマットを試す
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            # datetime.strptime は date part も必要とするが、time() で時間だけ抽出
            return datetime.strptime(s, fmt).time()
        except ValueError:
            pass

    # ':'区切りでパースを試みる（最後の手段）
    parts = s.split(":")
    if len(parts) >= 2:
        try:
            h = int(parts[0])
            m = int(parts[1])
            s = int(parts[2]) if len(parts) == 3 else 0
            # timeオブジェクトを直接作成
            return time(h, m, s)
        except ValueError:
            pass
            
    raise ValueError(f"Invalid time format: {s}")

def load_timetable() -> list[dict]:
    """TimeTable を読み込み、(period, start, end) の dict のリストを返す（時限昇順）。"""
    # ORMを使用して TimeTable からデータを取得
    rows = db.session.query(TimeTable).order_by(TimeTable.時限).all()
    
    result = []
    for r in rows:
        try:
            # データベースから取得した時刻文字列をパース
            start_t = _parse_hhmm_or_hhmmss(r.開始時刻)
            end_t = _parse_hhmm_or_hhmmss(r.終了時刻)
            
            result.append({
                "period": r.時限,
                "start": start_t,
                "end": end_t
            })
        except ValueError as e:
            print(f"Warning: Failed to parse time for period {r.時限}: {e}")
            continue
            
    return result

def resolve_period_for(ts_dt: datetime) -> Optional[dict]:
    """タイムスタンプが属する（または最も近い）時限を解決する。"""
    ttable = load_timetable()
    if not ttable:
        return None
    t = ts_dt.time()

    # 1. 範囲内の時限を探す
    for rec in ttable:
        if rec["start"] <= t < rec["end"]:
            return rec

    # 2. 範囲外の場合、最も近い時限を決定する
    first_rec = ttable[0]
    last_rec = ttable[-1]

    # 始業前の場合、最初の時限を返す
    if t < first_rec["start"]:
        return first_rec
    
    # 終業後の場合、最後の時限を返す
    if t >= last_rec["end"]:
        return last_rec

    # 休憩時間中の場合、次の時限を返す
    for i in range(len(ttable)-1):
        if ttable[i]["end"] <= t < ttable[i+1]["start"]:
            return ttable[i+1]
            
    return last_rec # フォールバック

def fetch_students():
    """List of students with gakka name."""
    with get_conn() as conn:
        # SQLAlchemyを使ってデータを取得する
        students = db.session.query(
            生徒.学科ID, 生徒.学生番号, 生徒.生徒名, 学科.学科名
        ).join(学科, 学科.学科ID == 生徒.学科ID).order_by(生徒.学科ID, 生徒.学生番号).all()
        return students

def fetch_timetable_for_week(gakka_id, period, week_day):
    """指定された学科ID、期、曜日の時間割を取得"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT 時限, 科目ID, 教室ID, 備考
            FROM 週時間割
            WHERE 学科ID = %s AND 期 = %s AND 曜日 = %s
            ORDER BY 時限
        """, (gakka_id, period, week_day))
        rows = cur.fetchall()
    return [{"時限": row[0], "科目ID": row[1], "教室ID": row[2], "備考": row[3]} for row in rows]
# =========================================================================
# app
# =========================================================================

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

# 💡 新規追加: submit エンドポイント
@app.route("/submit", methods=["POST"])
def submit():
    try:
        # フォームデータから学生番号と学科IDを取得（intに変換）
        学生番号 = int(request.form.get("student_no"))
        学科ID = int(request.form.get("gakka_id"))

        # タイムスタンプを正規化
        #  関数は元のファイルに存在すると仮定します
        ts = (request.form.get("ts_local") or request.form.get("ts"))

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

@app.route("/download")
@require_logs_auth
def download_csv():
    # クエリパラメータの取得
    start = request.args.get("start") or None
    end = request.args.get("end") or None
    student = request.args.get("student")
    gakka = request.args.get("gakka")
    学生番号 = int(student) if student else None
    学科ID = int(gakka) if gakka else None
    
    try:
        # CSVをメモリ上で生成
        buf = export_csv_to_memory(start, end, 学生番号, 学科ID)
        
        # ファイル名の設定
        fname = "入退室_全件.csv"
        if start or end or 学生番号 or 学科ID:
            tag = date.today().strftime("%Y%m%d")
            fname = f"入退室_条件付き_{tag}.csv"
        
        try:
            # Flask 2.0以降のsend_fileでファイルを返す
            return send_file(buf, as_attachment=True, download_name=fname,
                             mimetype="text/csv; charset=utf-8")
        except TypeError:
            # Flask 1.x系の場合の対応
            return send_file(buf, as_attachment=True, attachment_filename=fname,
                             mimetype="text/csv; charset=utf-8")

    except Exception as e:
        flash(f"CSV出力エラー: {e}")
        return redirect(url_for("index"))

@app.route("/reset_camlogs", methods=["POST"])
@require_logs_auth
def reset_camlogs():
    """カメラログの全削除"""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM カメラログ;")
            cur.execute("DELETE FROM sqlite_sequence WHERE name='カメラログ';")  # auto incrementリセット
            conn.commit()
        flash("✅ カメラログを全て削除しました。")
    except Exception as e:
        flash(f"⚠️ リセットエラー: {e}")
    return redirect(url_for("logs"))

@app.route("/api/add", methods=["POST"])
def api_add():
    try:
        # JSON または FORM のどちらにも対応
        data = request.get_json(silent=True) or request.form

        # パラメータの取得
        学生番号 = int(data.get("student"))
        学科ID = int(data.get("gakka"))
        ts = normalize_ts(data.get("ts"))

        # 学籍情報が正しいか確認（DBから名前を取得）
        official_name = get_official_student(学生番号, 学科ID)
        if not official_name:
            return jsonify({"ok": False, "error": "student not found"}), 400

        # 入退室記録の追加
        insert_attendance_input(学生番号, official_name, 学科ID, ts)

        return jsonify({"ok": True})

    except Exception as e:
        # 予期しないエラーは500として返す
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/add_by_names", methods=["POST"])
def api_add_by_names():
    """
    学科名（gakka_name）と学生番号（student）で1レコード追加。
    ts を省略した場合は TimeTable に基づいて「該当コマの開始1分前」に自動設定。
    """
    try:
        # JSON / form 両対応
        data = request.get_json(silent=True) or request.form

        gakka_name = (data.get("gakka_name") or "").strip()
        学生番号 = int(data.get("student"))

        # 学科名 → 学科ID を取得
        学科ID = get_gakka_id_by_name(gakka_name)
        if 学科ID is None:
            return jsonify({"ok": False, "error": "gakka not found"}), 400

        # 学生番号 + 学科ID から正式な生徒名を取得
        official_name = get_official_student(学生番号, 学科ID)
        if not official_name:
            return jsonify({"ok": False, "error": "student not found"}), 400

        # ts が指定されていれば整形、なければ自動決定
        ts = normalize_ts(data.get("ts"))
        if not ts:
            now = datetime.now()
            rec = resolve_period_for(now)  # 時限情報を返す関数（既存前提）

            if rec:
                # 該当コマの開始 1 分前
                start_dt = datetime.combine(date.today(), rec["start"])
                ts_dt = start_dt - timedelta(minutes=1)
                ts = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                # 時限情報が取れなかった場合は「今」
                ts = now.strftime("%Y-%m-%d %H:%M:%S")

        # 入退室レコードを1件追加（中で PostgreSQL に insert する想定）
        insert_attendance_input(学生番号, official_name, 学科ID, ts)

        return jsonify({"ok": True})

    except Exception as e:
        # 何か例外が起きたら 500 を返す
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/camlog", methods=["POST"])
def api_camlog():
    """
    Receive camera logs:
      status: 'detected' | 'ok' | 'lost' (required)
      marker, score, message, source(optional), ts(optional)
    """
    try:
        # JSONまたはformデータを受け取る
        data = request.get_json(silent=True) or request.form

        source  = (data.get("source") or "armarka").strip()
        status  = (data.get("status") or "").strip().lower()
        marker  = (data.get("marker") or "").strip() or None
        message = (data.get("message") or "").strip() or None
        score   = data.get("score")
        score   = float(score) if score not in (None, "") else None

        ts = normalize_ts(data.get("ts"))
        if not ts:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # statusが必須
        if not status:
            return jsonify({"ok": False, "error": "status required"}), 400

        # カメラログをデータベースに追加
        add_camlog(ts, source, status, marker, score, message)

        return jsonify({"ok": True})
    
    except Exception as e:
        # エラーが発生した場合、500エラーとして返す
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/reset_logs", methods=["POST"])
@require_logs_auth
def reset_logs():
    """入退室ログのリセット"""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM 入退室;")
            cur.execute("DELETE FROM sqlite_sequence WHERE name='入退室';")  # auto incrementリセット
            conn.commit()
        flash("✅ 入退室ログを全て削除しました。記録IDがリセットされました。")
    except Exception as e:
        flash(f"⚠️ リセットエラー: {e}")
    return redirect(url_for("index"))

@app.route("/login", methods=["GET", "POST"])
def logs_login():
    # next パラメータ（ログイン後の遷移先）
    next_url = request.args.get("next") or url_for("logs")
    
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == LOGS_PASSWORD:
            session["logs_ok"] = True
            flash("ログインしました。")
            return redirect(next_url)
        else:
            flash("パスワードが違います。")

    # シンプルなログイン画面
    return render_template_string("""
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>ログイン</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,'Hiragino Kaku Gothic ProN','Meiryo',sans-serif;margin:20px;background:#f7f7fb;}
.card{background:#fff;border-radius:12px;box-shadow:0 4px 12px rgba(0,0,0,.06);padding:16px;max-width:420px;margin:40px auto;}
label{display:block;font-size:12px;color:#555;margin:8px 0 4px;}
input,button{width:100%;padding:10px;border:1px solid #ddd;border-radius:8px;font-size:14px;}
button{background:#2f6feb;color:#fff;border:none;cursor:pointer;margin-top:10px}
button:hover{filter:brightness(.95)}
.flash{background:#fff3cd;border:1px solid #ffeeba;border-radius:8px;padding:10px;margin:0 0 12px}
a{text-decoration:none;color:#2f6feb;}
</style>
</head>
<body>
<div class="card">
  <h1 style="font-size:18px;margin:0 0 12px;">ログページ認証</h1>
  {% with messages = get_flashed_messages() %}
    {% if messages %}
      <div class="flash">
        {% for m in messages %}{{m}}<br>{% endfor %}
      </div>
    {% endif %}
  {% endwith %}
  <form method="post">
    <input type="hidden" name="next" value="{{ request.args.get('next','') }}">
    <label>パスワード</label>
    <input type="password" name="password" required>
    <button type="submit">ログイン</button>
  </form>
  <div style="margin-top:10px;"><a href="{{ url_for('index') }}">← 戻る</a></div>
</div>
</body>
</html>
""")

@app.route("/basic_week")
def basic_week():
    # クエリパラメータを取得
    year = request.args.get("year", 2025, type=int)
    gakka = request.args.get("gakka_id", 3, type=int)
    period = request.args.get("period", 1, type=int)

    # データベースから情報を取得
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT w.曜日, y.曜日名, w.時限, tt.開始時刻, tt.終了時刻,
                   COALESCE(sc.授業科目名, '') AS 科目名,
                   COALESCE(cr.教室名, '')     AS 教室名,
                   COALESCE(w.備考, '')        AS 備考
            FROM 週時間割 AS w
            JOIN 曜日マスタ AS y ON y.曜日ID = w.曜日
            JOIN TimeTable   AS tt ON tt.時限 = w.時限
            LEFT JOIN 授業科目 AS sc ON sc.授業科目ID = w.科目ID
            LEFT JOIN 教室     AS cr ON cr.教室ID     = w.教室ID
            WHERE w.年度 = %s AND w.学科ID = %s AND w.期 = %s
            ORDER BY w.曜日, w.時限
        """, (year, gakka, period))
        rows = cur.fetchall()

    # グリッド作成: 曜日(1-5) × 時限(1-5) → セル文字列「科目名（教室名）」
    days = [1, 2, 3, 4, 5]  # 月〜金
    periods = [1, 2, 3, 4, 5]
    grid = { (d, p): "" for d in days for p in periods }
    for r in rows:
        key = (r["曜日"], r["時限"])
        cell = r["科目名"] + (f"（{r['教室名']}）" if r["教室名"] else "")
        grid[key] = cell

    # 時間情報の取得
    times = { p: {
        "開始": next(r["開始時刻"] for r in rows if r["時限"] == p),
        "終了": next(r["終了時刻"] for r in rows if r["時限"] == p)
    } for p in set([r["時限"] for r in rows]) }

    # HTMLを生成して返す
    return render_template_string("""
    <!doctype html>
    <meta charset="utf-8">
    <title>週間基本予定表</title>
    <style>
      body{font-family:system-ui,Meiryo,sans-serif;margin:20px;background:#f7f7fb}
      table{border-collapse:collapse;width:100%;background:#fff;border-radius:12px;overflow:hidden}
      th,td{border:1px solid #eee;padding:10px;font-size:14px}
      th{background:#f3f6ff}
      .time{color:#666;font-size:12px}
      .head{display:flex;gap:8px;align-items:center;margin-bottom:10px}
      .head input,.head select{padding:6px 8px}
      .small{color:#666;font-size:12px;margin-top:6px}
    </style>
    <div class="head">
      <form method="get">
        年度 <input type="number" name="year" value="{{year}}" style="width:90px">
        学科ID <input type="number" name="gakka_id" value="{{gakka}}" style="width:80px">
        期 <input type="number" name="period" value="{{period}}" style="width:60px">
        <button>表示</button>
      </form>
    </div>
    <table>
      <thead>
        <tr>
          <th>時限</th>
          {% for d in [1, 2, 3, 4, 5] %}
            <th>{{ {1:"月",2:"火",3:"水",4:"木",5:"金"}[d] }}</th>
          {% endfor %}
        </tr>
      </thead>
      <tbody>
        {% for p in [1, 2, 3, 4, 5] %}
        <tr>
          <th>{{p}}限<br><span class="time">
            {{ times[p]["開始"] }}〜{{ times[p]["終了"] }}
          </span></th>
          {% for d in [1, 2, 3, 4, 5] %}
            <td>{{ grid[(d,p)] }}</td>
          {% endfor %}
        </tr>
        {% endfor %}
      </tbody>
    </table>
    <div class="small">年度={{year}} / 学科ID={{gakka}} / 期={{period}}</div>
    """, year=year, gakka=gakka, period=period, grid=grid, times=times)

@app.route("/schedule")
def schedule():
    """授業計画テーブルの一覧を表示"""
    with get_conn() as conn:
        cur = conn.cursor()

        # 期マスタ
        cur.execute("SELECT 期ID, 期名 FROM 期マスタ")
        periods = {row["期ID"]: row["期名"] for row in cur.fetchall()}

        # 曜日マスタ
        cur.execute("SELECT 曜日ID, 曜日名 FROM 曜日マスタ")
        weekdays = {row["曜日ID"]: row["曜日名"] for row in cur.fetchall()}

        # 授業計画
        cur.execute("""
            SELECT 日付, 期, 授業曜日, 備考
            FROM 授業計画
            ORDER BY DATE(日付)  -- 日付順に並べる（PostgreSQLでもOK）
        """)
        rows = cur.fetchall()

    # 授業計画の期IDを期名、授業曜日IDを曜日名に変換
    rows_with_period_and_weekday = []
    for row in rows:
        # RealDictCursor 前提なので row はすでに dict
        row_dict = dict(row)
        row_dict["期名"] = periods.get(row_dict["期"], "不明")
        row_dict["曜日名"] = weekdays.get(row_dict["授業曜日"], "不明")
        rows_with_period_and_weekday.append(row_dict)

    return render_template("schedule.html", rows=rows_with_period_and_weekday)

@app.route("/subject_rate", methods=["GET"])
def subject_rate():
    """
    生徒を選ぶだけで該当学科の全科目の出席率を表示。
    仕様:
      - グラフ画像は出さず、行ごとの進捗バーのみ表示
      - 「詳細を見る」→ /absent_reason に遷移（term, student_key, subject_id をクエリ付与）
      - 今日より前の未記入は欠席、今日以降の未記入は欠席に含めない（未記入）
      - 出席率の分母（総回数）は“今日より前の授業日”のみをカウント
    """
    import math
    from datetime import datetime, date as date_cls

    term = request.args.get("term", type=int, default=0)  # 0=全期
    student_key = request.args.get("student_key")         # "学生番号-学科ID"

    # UIマスタ
    students = fetch_students()  # Row: 学科ID, 学生番号, 生徒名, 学科名
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT 期ID, 期名
            FROM 期マスタ
            WHERE 期ID BETWEEN 1 AND 4
            ORDER BY 期ID
        """)
        terms = [{"期ID": 0, "期名": "全期(1-4)"}] + [dict(r) for r in cur.fetchall()]

    # termラベル
    term_label = "全期(1-4)" if term == 0 else next(
        (t["期名"] for t in terms if t["期ID"] == term),
        "未知の期"
    )

    # 生徒未選択 → フォームのみ表示
    if not student_key:
        return render_template("subject_rate.html", students=students, terms=terms)

    # 複合キー分解 "学生番号-学科ID"
    try:
        student_no_str, gakka_id_str = student_key.split("-", 1)
        student_no = int(student_no_str)
        gakka_id = int(gakka_id_str)
    except Exception:
        return "student_key の形式が不正です（例: 12345-3）。", 400

    # ===== マスタ/必要データ取得 =====
    with get_conn() as conn:
        cur = conn.cursor()

        # 生徒氏名
        cur.execute("""
            SELECT 生徒名
            FROM 生徒
            WHERE 学生番号 = %s AND 学科ID = %s
        """, (student_no, gakka_id))
        row = cur.fetchone()
        if not row:
            return "生徒マスタに存在しません（学生番号+学科ID）。", 400
        student_name = row["生徒名"]

        term_list = [term] if term in (1, 2, 3, 4) else [1, 2, 3, 4]
        q_marks = ",".join(["%s"] * len(term_list))

        # 授業計画（平日のみ）
        cur.execute(
            f"""
            SELECT 日付, 授業曜日, 期
            FROM 授業計画
            WHERE 期 IN ({q_marks}) AND 授業曜日 BETWEEN 1 AND 5
            ORDER BY 日付 ASC
            """,
            term_list,
        )
        plan_days = cur.fetchall()

        # 週時間割（学科×期×平日）
        cur.execute(
            f"""
            SELECT 期, 曜日, 時限, 科目ID, 教室ID, 備考
            FROM 週時間割
            WHERE 学科ID = %s AND 期 IN ({q_marks}) AND 曜日 BETWEEN 1 AND 5
            """,
            (gakka_id, *term_list),
        )
        wk = {
            (r["期"], r["曜日"], r["時限"]): {
                "科目ID": r["科目ID"],
                "教室ID": r["教室ID"],
                "教員名": (r["備考"] or "").strip(),
            }
            for r in cur.fetchall()
        }

        # 科目名／教室名
        cur.execute("SELECT 授業科目ID, 授業科目名 FROM 授業科目")
        subj_map = {r["授業科目ID"]: r["授業科目名"] for r in cur.fetchall()}

        cur.execute("SELECT 教室ID, 教室名 FROM 教室")
        room_map = {r["教室ID"]: r["教室名"] for r in cur.fetchall()}

        # TimeTable
        cur.execute("""
            SELECT 時限, 開始時刻, 終了時刻
            FROM TimeTable
            ORDER BY 時限
        """)
        tt = {r["時限"]: (r["開始時刻"], r["終了時刻"]) for r in cur.fetchall()}

        # 入室ログ（日付範囲）一括
        def _to_date(s):
            # PostgreSQL の DATE 型 / TIMESTAMP 型にも対応
            if isinstance(s, date_cls):
                return s
            if isinstance(s, datetime):
                return s.date()
            s2 = s.replace("/", "-")
            return datetime.strptime(s2, "%Y-%m-%d").date()

        if plan_days:
            dmin = _to_date(plan_days[0]["日付"])
            dmax = _to_date(plan_days[-1]["日付"])
            if dmax < dmin:
                dmin, dmax = dmax, dmin

            cur.execute(
                """
                SELECT 入退出時間
                FROM 入退室
                WHERE 学生番号 = %s
                  AND 学科ID   = %s
                  AND 入室区分 = '入室'
                  AND DATE(入退出時間) BETWEEN %s AND %s
                ORDER BY 入退出時間 ASC
                """,
                (student_no, gakka_id, dmin.isoformat(), dmax.isoformat()),
            )
            in_rows = cur.fetchall()
        else:
            in_rows = []

    # 日付→その日の入室時刻リスト
    per_day_ins = {}
    for r in in_rows:
        dt = datetime.strptime(r["入退出時間"], "%Y-%m-%d %H:%M:%S")
        per_day_ins.setdefault(dt.date().isoformat(), []).append(dt)

    def _parse_hms(s):
        # "HH:MM:SS" or "HH:MM" を time に
        s = s.strip()
        if len(s) == 8:
            return datetime.strptime(s, "%H:%M:%S").time()
        return datetime.strptime(s, "%H:%M").time()

    # ===== 集計 =====
    stats = {}  # subj_id -> dict
    today = datetime.now().date()

    for p in plan_days:
        # 日付/曜日の正規化
        d = _to_date(p["日付"])
        w = p["授業曜日"]
        t_in_day = per_day_ins.get(d.isoformat(), [])

        for period, (start_s, end_s) in tt.items():
            key = (p["期"], w, period)
            if key not in wk or not wk[key]["科目ID"]:
                continue

            subj_id = wk[key]["科目ID"]
            subj_name = subj_map.get(subj_id, f"科目{subj_id}")
            teacher = wk[key]["教員名"]
            room = room_map.get(wk[key]["教室ID"], "")

            start_dt = datetime.combine(d, _parse_hms(start_s))
            end_dt = datetime.combine(d, _parse_hms(end_s))

            first_in = next((x for x in t_in_day if x <= end_dt), None)

            # === 欠席/未記入/出席/遅刻の判定 ===
            if first_in is None:
                if d < today:
                    status = "欠席"     # 既に終わった授業日の未記入は欠席
                else:
                    status = "未記入"   # 今日以降は分母に入れない
            else:
                status = "出席" if first_in.time() <= start_dt.time() else "遅刻"

            # === 集計レコード取得/初期化 ===
            s = stats.setdefault(
                subj_id,
                {
                    "科目名": subj_name,
                    "教員名": teacher,
                    "教室例": room,
                    "出席": 0,
                    "遅刻": 0,
                    "欠席": 0,
                    "未記入": 0,
                    "総回数": 0,
                    "必要出席回数": 0,
                    "欠席日": set(),
                },
            )

            # 教員/教室は空なら上書き
            if not s["教員名"] and teacher:
                s["教員名"] = teacher
            if not s["教室例"] and room:
                s["教室例"] = room

            # 総回数は「今日より前の授業日」だけカウント（=分母）
            held = d < today
            if held:
                s["総回数"] += 1

            # カウント（未記入は分母・欠席どちらにも入らない）
            if status in ("出席", "遅刻", "欠席"):
                s[status] += 1
                if status == "欠席":
                    s["欠席日"].add(d.isoformat())
            else:
                s["未記入"] += 1

    # テーブル行を構成
    rows = []
    for subj_id, s in stats.items():
        total = max(s["総回数"], 1)  # 0割防止
        required = math.ceil(s["総回数"] * 0.8)
        rate = (s["出席"] / total) * 100.0
        rows.append(
            {
                "科目ID": subj_id,
                "科目名": s["科目名"],
                "教員名": s["教員名"],
                "教室例": s["教室例"],
                "出席": s["出席"],
                "遅刻": s["遅刻"],
                "欠席": s["欠席"],
                "未記入": s["未記入"],
                "総回数": s["総回数"],
                "必要出席回数": required,
                "出席率": rate,
                "欠席日一覧": sorted(list(s["欠席日"])),
            }
        )

    # 出席率降順 → 科目名昇順
    rows.sort(key=lambda r: (-r["出席率"], r["科目名"]))

    # 画面描画
    return render_template(
        "subject_rate2.html",
        terms=terms,
        students=students,
        term=term,
        student_key=student_key,
        student_name=student_name,
        term_label=term_label,
        rows=rows,
    )


@app.route("/weekly_schedule")
def weekly_schedule():
    """週時間割テーブルの一覧を表示"""
    with get_conn() as conn:
        cur = conn.cursor()

        # 授業科目
        cur.execute("SELECT 授業科目ID, 授業科目名 FROM 授業科目")
        subjects = {row["授業科目ID"]: row["授業科目名"] for row in cur.fetchall()}

        # 教室
        cur.execute("SELECT 教室ID, 教室名 FROM 教室")
        classrooms = {row["教室ID"]: row["教室名"] for row in cur.fetchall()}

        # 期マスタ
        cur.execute("SELECT 期ID, 期名 FROM 期マスタ")
        periods = {row["期ID"]: row["期名"] for row in cur.fetchall()}

        # 曜日マスタ
        cur.execute("SELECT 曜日ID, 曜日名 FROM 曜日マスタ")
        weekdays = {row["曜日ID"]: row["曜日名"] for row in cur.fetchall()}

        # 週時間割
        cur.execute("""
            SELECT 年度, 学科ID, 期, 曜日, 時限, 科目ID, 教室ID, 備考
            FROM 週時間割
            ORDER BY 曜日, 時限
        """)
        rows = cur.fetchall()

    # 週時間割のデータを整形
    rows_with_details = []
    for row in rows:
        row_dict = dict(row)  # RealDictCursor なので dict にしてOK
        row_dict["科目名"] = subjects.get(row_dict["科目ID"], "不明")
        row_dict["教室名"] = classrooms.get(row_dict["教室ID"], "不明")
        row_dict["期名"] = periods.get(row_dict["期"], "不明")
        row_dict["曜日名"] = weekdays.get(row_dict["曜日"], "不明")
        rows_with_details.append(row_dict)

    return render_template("weekly_schedule.html", rows=rows_with_details)

@app.route("/api/reset", methods=["POST"])
def api_reset():
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            # PostgreSQL では DELETE 文は SQLite と同じで OK
            cur.execute("DELETE FROM 入退室;")
            conn.commit()

        return jsonify({"ok": True, "message": "logs cleared"})

    except Exception as e:
        # もし何かトラブルが起きたら 500 を返す
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/kamoku_csv")
def kamoku_csv():
    """現在の科目・期の出席情報をCSVで出力"""
    subject_id = request.args.get("subject_id", type=int)
    term = request.args.get("term", type=int, default=0)

    if not subject_id:
        return "科目が選択されていません。", 400

    # --- CSV 作成 ---
    output = StringIO()

    # Excel対応：UTF-8 BOMを付ける
    output.write("\ufeff")

    writer = csv.writer(output)
    writer.writerow(["学生番号", "生徒名", "出席", "遅刻", "欠席", "未記入", "総回数", "出席率(%)"])
    writer.writerow(["---", "このCSV出力は動作確認用です。", "", "", "", "", "", ""])

    csv_data = output.getvalue()
    output.close()

    # --- Response で返す ---
    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=attendance_{subject_id}_{term}.csv"
        }
    )

@app.route("/kamoku_edit", methods=["GET"])
def kamoku_edit():
    """授業科目一覧 + 新規追加フォーム"""
    with get_conn() as conn:
        cur = conn.cursor()

        # 授業科目一覧（学科名付き）
        cur.execute("""
            SELECT 
              s.授業科目ID,
              s.授業科目名,
              s.学科ID      AS 科目学科ID,   -- 学科テーブルと衝突しないように別名
              s.単位,
              s.備考,
              g.学科名
            FROM 授業科目 s
            LEFT JOIN 学科 g ON g.学科ID = s.学科ID
            ORDER BY s.授業科目ID
        """)
        subjects = cur.fetchall()

    # 学科一覧（プルダウン用） ※これは別関数でPostgreSQL対応済み想定
    gakkas = fetch_gakkas()

    return render_template(
        "kamoku_edit.html",
        subjects=subjects,
        gakkas=gakkas,
    )

@app.route("/tukijikanwari", methods=["GET"])
def tukijikanwari():
    """
    月の予定表（列=曜日）カレンダー表示 + 授業科目編集ボタン
    """
    today = date.today()
    selected_month = request.args.get("month", default=today.month, type=int)
    selected_year  = request.args.get("year",  default=today.year,  type=int)

    # Render 用に作り直した generate_monthly_schedule(year, month) を呼び出し
    # 戻り値: { month: { day: [ {時限, 学科ID, 科目名, 教室ID, 備考}, ... ] } }
    monthly_schedule = generate_monthly_schedule(
        selected_month,
        selected_year,
    )
    days_map = monthly_schedule.get(selected_month, {})

    # 月曜始まりのカレンダー
    cal = calendar.Calendar(firstweekday=0)  # 0 = 月曜日
    month_weeks = cal.monthdayscalendar(selected_year, selected_month)

    youbi_labels = ["月", "火", "水", "木", "金", "土", "日"]

    return render_template(
        "tukijikanwari.html",
        selected_year=selected_year,
        selected_month=selected_month,
        month_weeks=month_weeks,
        days_map=days_map,
        youbi_labels=youbi_labels,
    )

@app.route("/kiki")
def kiki():
    """期マスタテーブルの一覧を表示"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT 期ID, 期名
            FROM 期マスタ
            ORDER BY 期ID
        """)
        rows = cur.fetchall()

    return render_template("kiki.html", rows=rows)


# ==============================
#  教室一覧
# ==============================
@app.route("/classrooms")
def classrooms():
    """教室テーブルの一覧を表示"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT 教室ID, 教室名, 収容人数
            FROM 教室
            ORDER BY 教室ID
        """)
        rows = cur.fetchall()

    return render_template("classrooms.html", rows=rows)

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

@app.route("/kamoku", methods=["GET"])
def kamoku():
    """授業科目を選択して生徒別の出席情報を表示（CSV出力ボタン付き）"""

    def parse_time_safe(s: str):
        s = s.strip()
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(s.zfill(5), fmt).time()
            except ValueError:
                continue
        raise ValueError(f"Invalid time format: {s}")

    # --- マスタ系の読み込み ---
    with get_conn() as conn:
        cur = conn.cursor()
        # 授業科目一覧
        cur.execute("""
            SELECT 授業科目ID, 授業科目名, 学科ID, 単位, 備考
            FROM 授業科目
            ORDER BY 授業科目ID
        """)
        subjects_all = cur.fetchall()

        # 期マスタ（1〜4期） + 先頭に「全期」
        cur.execute("""
            SELECT 期ID, 期名
            FROM 期マスタ
            WHERE 期ID BETWEEN 1 AND 4
            ORDER BY 期ID
        """)
        terms = [{"期ID": 0, "期名": "全期(1-4)"}] + [dict(r) for r in cur.fetchall()]

        # 時限ごとの開始・終了
        cur.execute("""
            SELECT 時限, 開始時刻, 終了時刻
            FROM TimeTable
            ORDER BY 時限
        """)
        tt = {r["時限"]: (r["開始時刻"], r["終了時刻"]) for r in cur.fetchall()}

    # --- クエリパラメータ ---
    subject_id = request.args.get("subject_id", type=int)
    term = request.args.get("term", type=int, default=0)

    # 科目未選択 → 科目選択画面だけ
    if not subject_id:
        return render_template(
            "kamoku.html",
            subjects_all=subjects_all,
            terms=terms
        )

    # --- 科目・生徒・授業計画など本体 ---
    with get_conn() as conn:
        cur = conn.cursor()

        # 対象科目の名称と学科ID
        cur.execute("""
            SELECT 授業科目名, 学科ID
            FROM 授業科目
            WHERE 授業科目ID = %s
        """, (subject_id,))
        subj = cur.fetchone()
        if not subj:
            return f"授業科目ID {subject_id} が見つかりません。", 404
        subject_name, gakka_id = subj["授業科目名"], subj["学科ID"]

        # 学科に属する生徒一覧
        cur.execute("""
            SELECT 学生番号, 生徒名
            FROM 生徒
            WHERE 学科ID = %s
            ORDER BY 学生番号
        """, (gakka_id,))
        students = cur.fetchall()

        # 対象期リスト（0=全期なら1〜4）
        term_list = [term] if term in (1, 2, 3, 4) else [1, 2, 3, 4]
        q_marks = ",".join(["%s"] * len(term_list))

        # 授業計画（日付・曜日・期）
        cur.execute(
            f"""
            SELECT 日付, 授業曜日, 期
            FROM 授業計画
            WHERE 期 IN ({q_marks})
            ORDER BY 日付
            """,
            term_list
        )
        plans = cur.fetchall()

        # 週時間割から「どの期×曜日×時限がどの科目IDか」のマップ
        cur.execute(
            f"""
            SELECT 期, 曜日, 時限, 科目ID
            FROM 週時間割
            WHERE 学科ID = %s AND 期 IN ({q_marks})
            """,
            (gakka_id, *term_list)
        )
        wk = {(r["期"], r["曜日"], r["時限"]): r["科目ID"] for r in cur.fetchall()}

        # 授業が行われる日付と、その日の開始・終了時刻のリストを作成
        def _to_date(s):
            return datetime.strptime(s.replace('/', '-'), "%Y-%m-%d").date()

        classes = []
        for p in plans:
            d = _to_date(p["日付"])
            w = p["授業曜日"]
            for period, (start_s, end_s) in tt.items():
                if wk.get((p["期"], w, period)) == subject_id:
                    start_dt = datetime.combine(d, parse_time_safe(start_s))
                    end_dt = datetime.combine(d, parse_time_safe(end_s))
                    classes.append((d, start_dt, end_dt))

        # 対象期間の「入室」ログを取得
        if classes:
            dmin = min(c[0] for c in classes).isoformat()
            dmax = max(c[0] for c in classes).isoformat()
            cur.execute(
                """
                SELECT 学生番号, 入退出時間
                FROM 入退室
                WHERE 学科ID = %s
                  AND 入室区分 = '入室'
                  AND DATE(入退出時間) BETWEEN %s AND %s
                """,
                (gakka_id, dmin, dmax)
            )
            in_rows = cur.fetchall()
        else:
            in_rows = []

    # --- 生徒×日付ごとの「入室時刻一覧」マップを作る ---
    per_student_day_ins = {}
    for r in in_rows:
        dt = datetime.strptime(r["入退出時間"], "%Y-%m-%d %H:%M:%S")
        key = (r["学生番号"], dt.date().isoformat())
        per_student_day_ins.setdefault(key, []).append(dt)

    today = datetime.now().date()
    rows = []

    # --- 生徒ごとに出席集計 ---
    for s in students:
        std_no, name = s["学生番号"], s["生徒名"]
        cnt = {"出席": 0, "遅刻": 0, "欠席": 0, "未記入": 0, "総回数": 0}
        for (d, start_dt, end_dt) in classes:
            if d < today:
                cnt["総回数"] += 1
            logs = per_student_day_ins.get((std_no, d.isoformat()), [])
            # 授業終了時刻までの最初の入室
            first_in = next((x for x in logs if x <= end_dt), None)
            if first_in is None:
                status = "欠席" if d < today else "未記入"
            else:
                status = "出席" if first_in.time() <= start_dt.time() else "遅刻"
            if status in cnt:
                cnt[status] += 1

        total = max(cnt["総回数"], 1)
        cnt["出席率"] = round(cnt["出席"] / total * 100, 1)
        rows.append(dict(学生番号=std_no, 生徒名=name, **cnt))

    rows.sort(key=lambda r: r["学生番号"])
    term_label = "全期(1-4)" if term == 0 else f"{term}期"

    return render_template(
        "kamoku2.html",
        subjects_all=subjects_all,
        terms=terms,
        subject_id=subject_id,
        subject_name=subject_name,
        gakka_id=gakka_id,
        term=term,
        term_label=term_label,
        rows=rows
    )

@app.route("/kamoku_edit/<int:subject_id>", methods=["GET"])
def kamoku_edit_form(subject_id: int):
    """授業科目の編集フォーム（Render / PostgreSQL 対応版）"""

    with get_conn() as conn:
        cur = conn.cursor()

        # PostgreSQL では ? → %s
        cur.execute("""
            SELECT 授業科目ID, 授業科目名, 学科ID, 単位, 備考
            FROM 授業科目
            WHERE 授業科目ID = %s
        """, (subject_id,))
        row = cur.fetchone()

        if not row:
            abort(404)

        # 学科一覧（プルダウン）
        gakkas = fetch_gakkas()

    return render_template_string("""
<!doctype html>
<meta charset="utf-8"><title>授業科目の編集</title>
<style>
body{font-family:system-ui,Meiryo,sans-serif;margin:20px;background:#f7f7fb}
.card{background:#fff;border-radius:12px;box-shadow:0 4px 12px rgba(0,0,0,.06);padding:16px;margin-bottom:16px}
h1{margin:0 0 12px}
input,select,button{padding:8px;border:1px solid #ddd;border-radius:8px;font-size:14px}
button{background:#2f6feb;color:#fff;border:none;cursor:pointer}
button:hover{filter:brightness(.95)}
.flex{display:flex;gap:10px;flex-wrap:wrap}
.small{color:#666;font-size:12px}
</style>

<div class="card">
  <h1>授業科目の編集（ID: {{ row['授業科目ID'] }}）</h1>
  <a href="{{ url_for('kamoku_edit') }}" class="small">← 一覧に戻る</a>
</div>

<div class="card">
  <form method="post" action="{{ url_for('kamoku_update', subject_id=row['授業科目ID']) }}" class="flex">
    <div>
      <label>授業科目名</label><br>
      <input name="name" value="{{ row['授業科目名'] }}" required style="min-width:280px">
    </div>

    <div>
      <label>学科</label><br>
      <select name="gakka_id" required>
        {% for g in gakkas %}
          <option value="{{ g['学科ID'] }}"
            {% if g['学科ID']==row['学科ID'] %}selected{% endif %}>
            {{ g['学科名'] }}（ID:{{ g['学科ID'] }}）
          </option>
        {% endfor %}
      </select>
    </div>

    <div>
      <label>単位</label><br>
      <input name="unit" type="number" min="0" value="{{ row['単位'] or 0 }}" style="width:90px">
    </div>

    <div style="flex:1 1 320px">
      <label>備考</label><br>
      <input name="note" value="{{ row['備考'] or '' }}">
    </div>

    <div style="align-self:flex-end">
      <button type="submit">更新</button>
    </div>
  </form>
</div>
""", row=row, gakkas=gakkas)

@app.route("/kamoku_add", methods=["POST"])
def kamoku_add():
    """授業科目の新規追加（Render / PostgreSQL 対応版）"""
    name = (request.form.get("name") or "").strip()
    gakka_id = _parse_int(request.form.get("gakka_id"))
    unit = _parse_int(request.form.get("unit"), 0)
    note = (request.form.get("note") or "").strip()

    if not name or gakka_id is None:
        flash("入力が不足しています。")
        return redirect(url_for("kamoku_edit"))

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            # SMALLINT 主キーを自前採番するヘルパー関数（既存実装を利用）
            new_id = _next_subject_id(conn)

            cur.execute("""
                INSERT INTO 授業科目
                  (授業科目ID, 授業科目名, 学科ID, 単位, 学科フラグ, 備考)
                VALUES (%s, %s, %s, %s, 0, %s)
            """, (new_id, name, gakka_id, unit, note))
            conn.commit()

        flash(f"科目を追加しました（ID: {new_id}）。")
    except Exception as e:
        flash(f"追加エラー: {e}")

    return redirect(url_for("kamoku_edit"))


@app.route("/kamoku_update/<int:subject_id>", methods=["POST"])
def kamoku_update(subject_id: int):
    """授業科目の更新（Render / PostgreSQL 対応版）"""
    name = (request.form.get("name") or "").strip()
    gakka_id = _parse_int(request.form.get("gakka_id"))
    unit = _parse_int(request.form.get("unit"), 0)
    note = (request.form.get("note") or "").strip()

    if not name or gakka_id is None:
        flash("入力が不足しています。")
        return redirect(url_for("kamoku_edit_form", subject_id=subject_id))

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE 授業科目
                SET 授業科目名 = %s,
                    学科ID     = %s,
                    単位       = %s,
                    備考       = %s
                WHERE 授業科目ID = %s
            """, (name, gakka_id, unit, note, subject_id))

            if cur.rowcount == 0:
                flash("対象の科目が見つかりません。")
            else:
                flash("更新しました。")

            conn.commit()
    except Exception as e:
        flash(f"更新エラー: {e}")

    return redirect(url_for("kamoku_edit"))

@app.route("/absent_reason", methods=["GET", "POST"])
def absent_reason():
    """
    科目ごとの欠席日に対して理由を登録するページ。
    必須: term(0=全期), student_key("学生番号-学科ID"), subject_id(科目ID)
    """
    term = request.values.get("term", type=int, default=0)
    student_key = request.values.get("student_key")
    subject_id = request.values.get("subject_id", type=int)

    if not student_key or subject_id is None:
        return "必要なパラメータが不足しています (student_key, subject_id)", 400

    try:
        student_no_str, gakka_id_str = student_key.split("-", 1)
        学生番号 = int(student_no_str)
        学科ID = int(gakka_id_str)
    except Exception:
        return "student_key の形式が不正です（例: 12345-3）。", 400

    # ===== 生徒名・科目名・マスタの取得 =====
    with get_conn() as conn:
        cur = conn.cursor()

        # 生徒名
        cur.execute(
            """
            SELECT 生徒名
            FROM 生徒
            WHERE 学生番号 = %s AND 学科ID = %s
            """,
            (学生番号, 学科ID),
        )
        row = cur.fetchone()
        if not row:
            return "生徒マスタに存在しません。", 400
        student_name = row["生徒名"]

        # 科目名
        cur.execute(
            """
            SELECT 授業科目名
            FROM 授業科目
            WHERE 授業科目ID = %s
            """,
            (subject_id,),
        )
        row2 = cur.fetchone()
        subject_name = row2["授業科目名"] if row2 else f"科目{subject_id}"

        # 期マスタ（ラベル用）
        cur.execute(
            """
            SELECT 期ID, 期名
            FROM 期マスタ
            WHERE 期ID BETWEEN 1 AND 4
            ORDER BY 期ID
            """
        )
        terms = [{"期ID": 0, "期名": "全期(1-4)"}] + [dict(r) for r in cur.fetchall()]
        term_label = (
            "全期(1-4)"
            if term == 0
            else next((t["期名"] for t in terms if t["期ID"] == term), "未知の期")
        )

        # 絞り込む期リスト
        term_list = [term] if term in (1, 2, 3, 4) else [1, 2, 3, 4]
        q_marks = ",".join(["%s"] * len(term_list))

        # 授業計画（該当期×平日）
        cur.execute(
            f"""
            SELECT 日付, 授業曜日, 期
            FROM 授業計画
            WHERE 期 IN ({q_marks}) AND 授業曜日 BETWEEN 1 AND 5
            ORDER BY 日付 ASC
            """,
            term_list,
        )
        plan_days = cur.fetchall()

        # 週時間割（指定科目のみ）
        cur.execute(
            f"""
            SELECT 期, 曜日, 時限, 科目ID
            FROM 週時間割
            WHERE 学科ID = %s AND 期 IN ({q_marks}) AND 曜日 BETWEEN 1 AND 5
              AND 科目ID = %s
            """,
            (学科ID, *term_list, subject_id),
        )
        wk = {(r["期"], r["曜日"], r["時限"]): True for r in cur.fetchall()}

        # TimeTable
        cur.execute(
            """
            SELECT 時限, 開始時刻, 終了時刻
            FROM TimeTable
            ORDER BY 時限
            """
        )
        tt = {r["時限"]: (r["開始時刻"], r["終了時刻"]) for r in cur.fetchall()}

        # 入室ログ（対象期間）
        def _to_date(v):
            if isinstance(v, date_cls):
                return v
            if isinstance(v, datetime):
                return v.date()
            s = str(v)
            s2 = s.replace("/", "-")
            return datetime.strptime(s2, "%Y-%m-%d").date()

        if plan_days:
            dmin = _to_date(plan_days[0]["日付"])
            dmax = _to_date(plan_days[-1]["日付"])
            if dmax < dmin:
                dmin, dmax = dmax, dmin

            cur.execute(
                """
                SELECT 入退出時間
                FROM 入退室
                WHERE 学生番号 = %s
                  AND 学科ID   = %s
                  AND 入室区分 = '入室'
                  AND DATE(入退出時間) BETWEEN %s AND %s
                ORDER BY 入退出時間 ASC
                """,
                (学生番号, 学科ID, dmin.isoformat(), dmax.isoformat()),
            )
            in_rows = cur.fetchall()
        else:
            in_rows = []

    # ===== 日付→その日の入室リスト =====
    per_day_ins = {}
    for r in in_rows:
        dt = datetime.strptime(r["入退出時間"], "%Y-%m-%d %H:%M:%S")
        per_day_ins.setdefault(dt.date().isoformat(), []).append(dt)

    def _parse_hms(s: str):
        s = s.strip()
        return (
            datetime.strptime(s, "%H:%M:%S").time()
            if len(s) == 8
            else datetime.strptime(s, "%H:%M").time()
        )

    # ===== 欠席日抽出 =====
    absent_dates = []
    for p in plan_days:
        d = _to_date(p["日付"])
        w = p["授業曜日"]
        t_in_day = per_day_ins.get(d.isoformat(), [])
        found_subject_on_day = False
        attended_this_subject = False

        for period, (start_s, end_s) in tt.items():
            key = (p["期"], w, period)
            if key not in wk:
                continue
            found_subject_on_day = True
            end_dt = datetime.combine(d, _parse_hms(end_s))
            # この日の最初の入室が終了時刻までにあるかどうか
            first_in = next((x for x in t_in_day if x <= end_dt), None)
            if first_in is not None:
                attended_this_subject = True
                break  # その日のこの科目は欠席ではない

        if found_subject_on_day and not attended_this_subject:
            absent_dates.append(d.isoformat())

    # ===== POST: 理由の保存 =====
    if request.method == "POST":
        saved = 0
        # name="reason[YYYY-MM-DD]" / name="other[YYYY-MM-DD]"
        for k in request.form.keys():
            if not k.startswith("reason[") or not k.endswith("]"):
                continue
            day = k[len("reason[") : -1]
            reason = request.form.get(k, "")
            other_text = (
                request.form.get(f"other[{day}]", "").strip()
                if reason == "その他"
                else ""
            )
            if day in absent_dates and reason in ("病欠", "公欠", "寝坊", "その他"):
                upsert_absent_reason(学生番号, 学科ID, subject_id, day, reason, other_text)
                saved += 1

        flash(f"{saved} 件保存しました。")
        # 保存後も同ページに戻る
        return redirect(
            url_for(
                "absent_reason",
                term=term,
                student_key=student_key,
                subject_id=subject_id,
            )
        )

    # 既存の理由（プリセット）
    preset = fetch_absent_reasons_map(学生番号, 学科ID, subject_id)

    # 画面レンダリング
    return render_template(
        "absent_reason.html",
        term=term,
        student_key=student_key,
        subject_id=subject_id,
        student_name=student_name,
        gakka_id=学科ID,
        subject_name=subject_name,
        term_label=term_label,
        absent_dates=absent_dates,
        preset=preset,
    )

@app.route("/jikanwari", methods=["GET"])
def jikanwari():
    """選択した期（1期〜4期）の時間割を曜日 × 時限の形式で表示"""

    # クエリパラメータから選択された期を取得（デフォルトは1期）
    selected_term = request.args.get("term", 1, type=int)

    with get_conn() as conn:
        cur = conn.cursor()

        # 曜日マスタを取得
        cur.execute("SELECT 曜日ID, 曜日名 FROM 曜日マスタ")
        weekdays = {row["曜日ID"]: row["曜日名"] for row in cur.fetchall()}

        # 週時間割のデータを取得（選択された期に該当するデータ）
        cur.execute(
            """
            SELECT 年度, 学科ID, 期, 曜日, 時限, 科目ID, 教室ID, 備考
            FROM 週時間割
            WHERE 期 = %s
            ORDER BY 時限, 曜日
            """,
            (selected_term,),
        )
        rows = cur.fetchall()

        # 授業科目と教室情報を取得
        cur.execute("SELECT 授業科目ID, 授業科目名 FROM 授業科目")
        subjects = {row["授業科目ID"]: row["授業科目名"] for row in cur.fetchall()}

        cur.execute("SELECT 教室ID, 教室名 FROM 教室")
        classrooms = {row["教室ID"]: row["教室名"] for row in cur.fetchall()}

        # 期マスタ（1〜4期）を取得
        cur.execute("SELECT 期ID, 期名 FROM 期マスタ WHERE 期ID BETWEEN 1 AND 4 ORDER BY 期ID")
        terms = {row["期ID"]: row["期名"] for row in cur.fetchall()}

    # 時間割を「時限 × 曜日」の形式に整形（1〜5限 × 全曜日）
    # schedule[時限][曜日名] = { 科目, 教員, 教室 }
    schedule = {
        period: {
            weekdays.get(d, "不明"): {"科目": "", "教員": "", "教室": ""}
            for d in weekdays  # d は 曜日ID
        }
        for period in range(1, 6)
    }

    # 時間割データを埋め込む
    for row in rows:
        day_name = weekdays.get(row["曜日"], "不明")
        period = row["時限"]
        subject = subjects.get(row["科目ID"], "不明")
        classroom = classrooms.get(row["教室ID"], "不明")
        teacher = row["備考"] or ""  # 備考欄を教員名として利用（空なら空文字）

        if period in schedule and day_name in schedule[period]:
            schedule[period][day_name] = {
                "科目": subject,
                "教員": teacher,
                "教室": classroom,
            }

    return render_template(
        "jikanwari.html",
        weekdays=weekdays,        # {曜日ID: 曜日名}
        schedule=schedule,        # 時限×曜日の2次元マップ
        terms=terms,              # {期ID: 期名}
        selected_term=selected_term,
    )

@app.route("/tukijikanwari_csv", methods=["GET"])
def tukijikanwari_csv():
    """
    /tukijikanwari の月カレンダーを CSV 出力
    出力列: 年, 月, 日, 曜日, 時限, 科目名, 教室ID, 備考
    """
    today = date.today()
    year  = request.args.get("year",  default=today.year,  type=int)
    month = request.args.get("month", default=today.month, type=int)

    # 既存関数を再利用
    # generate_monthly_schedule(selected_month, selected_year) を想定
    # 戻り値: { month: { day: [ {時限, 科目名, 教室ID, 備考, ...}, ... ] } }
    monthly_schedule = generate_monthly_schedule(selected_month=month, selected_year=year)
    days_map = monthly_schedule.get(month, {})

    youbi_names = ["月", "火", "水", "木", "金", "土", "日"]

    # CSV 構築
    buf = io.StringIO()
    writer = csv.writer(buf)

    # Excel で文字化けしないように UTF-8 BOM 付き
    writer.writerow(["年", "月", "日", "曜日", "時限", "科目名", "教室ID", "備考"])

    # 日付昇順 → 時限昇順で出力
    for d in sorted(days_map.keys()):
        try:
            w = date(year, month, int(d)).weekday()  # 0=月 … 6=日
            youbi = youbi_names[w]
        except Exception:
            youbi = ""
        lessons = sorted(days_map[d], key=lambda x: x.get("時限", 0))
        for les in lessons:
            writer.writerow([
                year,
                month,
                d,
                youbi,
                les.get("時限", ""),
                les.get("科目名", ""),
                les.get("教室ID", ""),
                les.get("備考", ""),
            ])

    data = buf.getvalue().encode("utf-8-sig")  # BOM付き
    bio = io.BytesIO(data)
    bio.seek(0)
    fname = f"月間時間割_{year}{month:02d}.csv"

    # Flask 2.x 以降の send_file
    try:
        return send_file(
            bio,
            as_attachment=True,
            download_name=fname,
            mimetype="text/csv; charset=utf-8",
        )
    except TypeError:
        # 古い Flask 用フォールバック
        return send_file(
            bio,
            as_attachment=True,
            attachment_filename=fname,
            mimetype="text/csv; charset=utf-8",
        )

@app.route("/kamoku_delete/<int:subject_id>", methods=["POST"])
def kamoku_delete(subject_id: int):
    """授業科目の削除（Render / PostgreSQL対応）"""
    try:
        with get_conn() as conn:
            cur = conn.cursor()

            # SQLite の ? → PostgreSQL の %s に変更
            cur.execute("""
                DELETE FROM 授業科目
                WHERE 授業科目ID = %s
            """, (subject_id,))

            if cur.rowcount == 0:
                flash("対象の科目が見つかりません。")
            else:
                flash("削除しました。")

            conn.commit()

    except Exception as e:
        flash(f"削除エラー: {e}")

    return redirect(url_for("kamoku_edit"))

@app.route("/edit_subject_dayperiod", methods=["GET", "POST"])
def edit_subject_dayperiod():
    ensure_special_schedule()

    y        = request.values.get("year",  type=int)
    m        = request.values.get("month", type=int)
    d        = request.values.get("day",   type=int)
    period   = request.values.get("period", type=int)
    gakka_id = request.values.get("gakka_id", type=int)

    if not all([y, m, d, period, gakka_id]):
        return "year, month, day, period, gakka_id は必須です。", 400

    target_date = date(y, m, d).isoformat()

    # ===== マスタ取得 =====
    with get_conn() as conn:
        cur = conn.cursor()

        # 授業科目一覧
        cur.execute("""
            SELECT 授業科目ID, 授業科目名
            FROM 授業科目
            ORDER BY 授業科目ID
        """)
        subjects = cur.fetchall()

        # 教室一覧
        cur.execute("""
            SELECT 教室ID, 教室名
            FROM 教室
            ORDER BY 教室ID
        """)
        rooms = cur.fetchall()

        # 既存の特別時間割
        cur.execute("""
            SELECT 科目ID, 教室ID, 備考
            FROM 特別時間割
            WHERE 日付 = %s AND 学科ID = %s AND 時限 = %s
        """, (target_date, gakka_id, period))
        special = cur.fetchone()

        # 授業計画から当日の「期×曜日」を取得
        # （PostgreSQL なので DATE(...) 関数は使わず、そのまま比較）
        cur.execute("""
            SELECT 期, 授業曜日
            FROM 授業計画
            WHERE 日付 = %s
            LIMIT 1
        """, (target_date,))
        jp = cur.fetchone()

        default_row = None
        if jp:
            cur.execute("""
                SELECT 科目ID, 教室ID, 備考
                FROM 週時間割
                WHERE 学科ID = %s AND 期 = %s AND 曜日 = %s AND 時限 = %s
            """, (gakka_id, jp["期"], jp["授業曜日"], period))
            default_row = cur.fetchone()

    # ===== POST: 保存 or 削除 =====
    if request.method == "POST":
        action  = request.form.get("action", "save")
        subj_id = request.form.get("科目ID", type=int)
        room_id = request.form.get("教室ID", type=int)
        note    = (request.form.get("備考") or "").strip()

        with get_conn() as conn:
            cur = conn.cursor()

            if action == "delete":
                # 特別時間割レコード削除 → 週時間割に戻す
                cur.execute("""
                    DELETE FROM 特別時間割
                    WHERE 日付 = %s AND 学科ID = %s AND 時限 = %s
                """, (target_date, gakka_id, period))
                conn.commit()
                flash("特別時間割を削除しました（週時間割に戻ります）。")

            else:
                # UPSERT (PostgreSQL の ON CONFLICT)
                cur.execute("""
                    INSERT INTO 特別時間割
                      (日付, 学科ID, 時限, 科目ID, 教室ID, 備考)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (日付, 学科ID, 時限)
                    DO UPDATE SET
                      科目ID  = EXCLUDED.科目ID,
                      教室ID  = EXCLUDED.教室ID,
                      備考    = EXCLUDED.備考
                """, (target_date, gakka_id, period, subj_id, room_id, note))
                conn.commit()
                flash("保存しました。")

        # 編集前の月へ戻る
        return redirect(url_for("tukijikanwari", year=y, month=m))

    # ===== GET: 画面表示用 初期値（特別時間割 > 週時間割） =====
    init_subj = special["科目ID"] if special else (default_row["科目ID"] if default_row else None)
    init_room = special["教室ID"] if special else (default_row["教室ID"] if default_row else None)
    init_note = special["備考"]   if special else (default_row["備考"]   if default_row else "")

    return render_template_string("""
<!doctype html><meta charset="utf-8">
<title>日付×時限×学科の授業科目 編集</title>
<style>
 body{font-family:system-ui,Meiryo,sans-serif;margin:20px;background:#f7f7fb}
 .card{background:#fff;border-radius:12px;box-shadow:0 4px 12px rgba(0,0,0,.06);padding:16px;max-width:600px}
 label{display:block;font-size:12px;color:#555;margin:8px 0 4px}
 select,input,textarea,button{width:100%;padding:10px;border:1px solid #ddd;border-radius:8px;font-size:14px}
 button{background:#2f6feb;color:#fff;border:none;cursor:pointer;margin-top:10px}
 button:hover{filter:brightness(.95)}
 .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
 .danger{background:#f44336}
 .small{font-size:12px;color:#666;margin-top:6px}
 a.btn{display:inline-block;padding:8px 12px;background:#888;color:#fff;border-radius:8px;text-decoration:none;margin-top:10px}
</style>
<div class="card">
  <h2 style="margin:0 0 10px">
    授業科目の編集：{{ y }}年{{ m }}月{{ d }}日／{{ period }}限／学科ID {{ gakka_id }}
  </h2>

  <form method="post">
    <div>
      <label>科目</label>
      <select name="科目ID" required>
        <option value="">-- 選択 --</option>
        {% for s in subjects %}
          <option value="{{ s['授業科目ID'] }}"
            {% if init_subj and s['授業科目ID'] == init_subj %}selected{% endif %}>
            {{ s['授業科目ID'] }} : {{ s['授業科目名'] }}
          </option>
        {% endfor %}
      </select>
    </div>

    <div class="row">
      <div>
        <label>教室</label>
        <select name="教室ID">
          <option value="">-- 未設定 --</option>
          {% for r in rooms %}
            <option value="{{ r['教室ID'] }}"
              {% if init_room and r['教室ID'] == init_room %}selected{% endif %}>
              {{ r['教室ID'] }} : {{ r['教室名'] }}
            </option>
          {% endfor %}
        </select>
      </div>
      <div>
        <label>備考</label>
        <input name="備考" value="{{ init_note or '' }}">
      </div>
    </div>

    <button type="submit" name="action" value="save">保存</button>
  </form>

  {% if init_subj is not none or init_room is not none or init_note %}
  <form method="post"
        onsubmit="return confirm('特別時間割を削除して週時間割に戻します。よろしいですか？');">
    <button class="danger" type="submit" name="action" value="delete">
      特別時間割を削除
    </button>
  </form>
  {% endif %}

  <a class="btn" href="{{ url_for('tukijikanwari', year=y, month=m) }}">← 月表示に戻る</a>
  <div class="small">
    ※ この変更は <b>{{ y }}-{{ '%02d'|format(m) }}-{{ '%02d'|format(d) }}</b> の該当コマだけに適用されます。
  </div>
</div>
""",
        y=y, m=m, d=d,
        period=period,
        gakka_id=gakka_id,
        subjects=subjects,
        rooms=rooms,
        init_subj=init_subj,
        init_room=init_room,
        init_note=init_note,
    )

@app.route("/timetable")
def timetable():
    # クエリパラメータの取得（デフォルト値を設定）
    gakka_id = request.args.get('gakka_id', 3, type=int)  # 学科ID（デフォルト 3）
    period = request.args.get('period', 1, type=int)  # 期（デフォルト 1）
    week_day = request.args.get('week_day', 1, type=int)  # 曜日（デフォルト 月曜日）

    # 時間割データを取得
    timetable_data = fetch_timetable_for_week(gakka_id, period, week_day)
    
    # HTMLを生成して返す
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>時間割</title>
        <style>
            body { font-family: system-ui, sans-serif; margin: 20px; background: #f4f4f4; }
            table { width: 100%; border-collapse: collapse; margin-top: 20px; }
            th, td { padding: 8px; text-align: left; border: 1px solid #ddd; }
            th { background-color: #f2f2f2; }
            td { background-color: #fff; }
        </style>
    </head>
    <body>
        <h1>時間割</h1>
        <h3>{{ period }}期 - {{ week_day }}曜日</h3>
        <table>
            <thead>
                <tr>
                    <th>時限</th>
                    <th>科目ID</th>
                    <th>教室ID</th>
                    <th>備考</th>
                </tr>
            </thead>
            <tbody>
                {% for row in timetable_data %}
                <tr>
                    <td>{{ row['時限'] }}</td>
                    <td>{{ row['科目ID'] }}</td>
                    <td>{{ row['教室ID'] }}</td>
                    <td>{{ row['備考'] }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </body>
    </html>
    """, timetable_data=timetable_data, period=period, week_day=week_day)

@app.route("/kamoku_edit_day", methods=["GET", "POST"])
def kamoku_edit_day():
    """
    特定の日付・時限の授業科目を編集
    - GET: 現在の科目を表示
    - POST: 選択した科目に更新
    """
    year   = request.args.get("year", type=int)
    month  = request.args.get("month", type=int)
    day    = request.args.get("day", type=int)
    period = request.args.get("period", type=int)

    # 一応日付として構成しておく（今は使っていないが将来拡張用）
    target_date = date(year, month, day)

    # ===== POST: 科目の更新 =====
    if request.method == "POST":
        # フォームから選択された科目IDを取得
        subject_raw = request.form.get("subject_id")
        try:
            new_subject_id = int(subject_raw)
        except (TypeError, ValueError):
            flash("科目IDが不正です。")
            return redirect(url_for("kamoku_edit_day",
                                    year=year, month=month, day=day, period=period))

        with get_conn() as conn:
            cur = conn.cursor()
            # この日付・時限・学科IDを特定して UPDATE するロジックは簡略化
            # 実際には generate_monthly_schedule() が参照する情報に合わせて条件を増やす想定
            cur.execute(
                """
                UPDATE 週時間割
                SET 科目ID = %s
                WHERE 時限 = %s AND 科目ID IS NOT NULL
                """,
                (new_subject_id, period),
            )
            conn.commit()

        flash(f"{year}年{month}月{day}日 {period}限の授業科目を更新しました。")
        return redirect(url_for("tukijikanwari", month=month, year=year))

    # ===== GET: 現在の科目・科目一覧を表示 =====
    with get_conn() as conn:
        cur = conn.cursor()

        # 授業科目一覧
        cur.execute("""
            SELECT 授業科目ID, 授業科目名
            FROM 授業科目
            ORDER BY 授業科目ID
        """)
        subjects = cur.fetchall()

        # 現在の科目（簡易版：同じ時限のものから1件だけ拾う）
        cur.execute(
            """
            SELECT w.科目ID, s.授業科目名
            FROM 週時間割 w
            LEFT JOIN 授業科目 s ON s.授業科目ID = w.科目ID
            WHERE w.時限 = %s
            LIMIT 1
            """,
            (period,),
        )
        current = cur.fetchone()

    return render_template_string(
        """
<!doctype html>
<meta charset="utf-8">
<title>授業科目の編集</title>
<style>
body{font-family:system-ui,Meiryo,sans-serif;margin:20px;background:#f7f7fb}
.card{background:#fff;border-radius:12px;box-shadow:0 4px 12px rgba(0,0,0,.06);padding:16px;margin-bottom:16px}
h1{margin:0 0 12px}
select,button{padding:8px;border:1px solid #ddd;border-radius:8px;font-size:14px}
button{background:#2f6feb;color:#fff;border:none;cursor:pointer}
button:hover{filter:brightness(.95)}
a{text-decoration:none;color:#2f6feb}
</style>

<div class="card">
  <h1>{{year}}年{{month}}月{{day}}日 {{period}}限 の授業科目を編集</h1>
  <a href="{{ url_for('tukijikanwari', year=year, month=month) }}">← 月間時間割に戻る</a>
</div>

<div class="card">
  <form method="post">
    <label>授業科目を選択</label><br>
    <select name="subject_id" required>
      {% for s in subjects %}
        <option value="{{ s['授業科目ID'] }}"
          {% if current and current['科目ID'] == s['授業科目ID'] %}selected{% endif %}>
          {{ s['授業科目名'] }}（ID:{{ s['授業科目ID'] }}）
        </option>
      {% endfor %}
    </select>
    <br><br>
    <button type="submit">更新</button>
  </form>
</div>
""",
        year=year,
        month=month,
        day=day,
        period=period,
        current=current,
        subjects=subjects,
    )

@app.route("/summary")
def summary():
    # ここで必要な関数を定義する（web_summary_functions.py ではなく、直接このファイル内で）
    from datetime import date

    # デフォルトの期間（今月1日〜今日）
    def default_month_range():
        start = date(date.today().year, date.today().month, 1)
        end = date.today()
        return start, end

    # フォームから取得
    student_no = request.values.get("student_no")
    gakka_id_str = request.values.get("gakka_id")
    start_date = request.values.get("start") or start_default
    end_date = request.values.get("end") or end_default

    totals = None
    daily = []
    subject_rates = []
    selected_student_name = None
    selected_gakka_name = None
    attendance_details = []

    if student_no and gakka_id_str:
        try:
            学生番号 = int(student_no)
            学科ID = int(gakka_id_str)

            # 1. 生徒名を取得
            selected_student_name = get_official_student(学生番号, 学科ID)

            # 2. 学科名を取得
            selected_gakka_name = next(
                (g["学科名"] for g in gakkas if g["学科ID"] == 学科ID),
                f"ID:{学科ID}"
            )

            # 3. 集計データを取得
            totals = fetch_attendance_totals(学生番号, 学科ID, start_date, end_date)
            daily = fetch_daily_first_checkin(学生番号, 学科ID, start_date, end_date)
            attendance_details = fetch_attendance_details(学生番号, 学科ID, start_date, end_date)
            subject_rates = fetch_subject_attendance_rates(学生番号, 学科ID, start_date, end_date)

        except Exception as e:
            print(f"サマリー取得エラー: {e}")  # ログ出力に切り替え（本番では flash 使う）

    # テンプレートをレンダリング
    return render_template(
        "summary.html",
        students=students,
        gakkas=gakkas,
        totals=totals,
        daily=daily,
        attendance_details=attendance_details,
        subject_rates=subject_rates,
        selected_student_name=selected_student_name,
        selected_gakka_name=selected_gakka_name,
        start_date=start_date,
        end_date=end_date,
        start_default=start_default,
        end_default=end_default,
        db_path=DATABASE_URL,  # Render環境向け
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


