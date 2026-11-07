"""오늘 어디 아파? | streamlit run app.py
GitHub: 이 파일과 requirements.txt를 올려 Streamlit Community Cloud에서 배포.
가족 공간 생성 후 가족 ID와 비밀번호를 별도 전달. QR에는 가족 ID만 포함.
알림은 앱 내 표시이며 백그라운드 푸시/SMS나 응급 감시 서비스가 아닙니다.
SQLite 로컬 저장소는 Cloud 재시작/재배포 시 보존되지 않을 수 있습니다.
영구 운영 전에는 관리형 DB, 접근 통제, 개인정보 처리·보관 정책이 필요합니다.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import qrcode
import streamlit as st
import plotly.graph_objects as go

DB_PATH = os.environ.get("CARE_DB_PATH", "family_care.sqlite3")
TZ = ZoneInfo("Asia/Seoul")

PARTS = [
    "머리",
    "목",
    "왼쪽 어깨",
    "오른쪽 어깨",
    "가슴",
    "배",
    "허리",
    "왼쪽 팔·손",
    "오른쪽 팔·손",
    "왼쪽 무릎",
    "오른쪽 무릎",
    "왼쪽 발·발목",
    "오른쪽 발·발목",
]

LEVELS = {
    1: "🙂 살짝",
    2: "😟 중간",
    3: "😣 많이",
}

DISCLAIMER = (
    "이 앱은 진단·치료·응급 감시 도구가 아닙니다. "
    "3단계는 간편 불편도이며, 임상 NPRS(0~10) 또는 VAS 점수가 아닙니다."
)


def now():
    return datetime.now(TZ)


def connect():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS families (
                id TEXT PRIMARY KEY,
                salt TEXT NOT NULL,
                digest TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id TEXT PRIMARY KEY,
                family TEXT NOT NULL REFERENCES families(id),
                person TEXT NOT NULL,
                part TEXT NOT NULL,
                level INTEGER NOT NULL CHECK(level BETWEEN 0 AND 3),
                stamp TEXT NOT NULL,
                seen INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_family_time
            ON entries(family, stamp)
            """
        )


def digest(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        bytes.fromhex(salt),
        260000,
    ).hex()


def create_family(password):
    fid = uuid.uuid4().hex
    salt = secrets.token_hex(16)

    with connect() as con:
        con.execute(
            "INSERT INTO families VALUES (?,?,?)",
            (fid, salt, digest(password, salt)),
  
