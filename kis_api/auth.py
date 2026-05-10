"""
kis_api/auth.py — KIS OAuth 토큰 관리

원칙:
- 토큰은 파일 캐시(token_cache.json)로 관리
- 만료 30분 전 자동 갱신
- KIS_IS_PAPER=True 시 모의투자 엔드포인트 자동 전환
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)

TOKEN_REFRESH_MARGIN_MINUTES = 30  # 만료 30분 전 갱신


class KISAuth:
    """KIS API OAuth 토큰 관리 클래스"""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        account_no: str,
        base_url: str,
        is_paper: bool,
    ) -> None:
        self.app_key = app_key
        self.app_secret = app_secret
        self.account_no = "".join(filter(str.isdigit, account_no))
        self.base_url = base_url
        self.is_paper = is_paper
        self.cache_file = Path(f"token_cache_{'paper' if is_paper else 'real'}.json")
        self._token: Optional[str] = None
        self._expires_at: Optional[datetime] = None

    # ── 공개 메서드 ─────────────────────────────────────────────────────────

    async def get_token(self) -> str:
        """유효한 액세스 토큰을 반환한다. 필요 시 갱신한다."""
        if self._is_token_valid():
            return self._token  # type: ignore[return-value]

        # 캐시 파일에서 먼저 로드 시도
        cached = self._load_cache()
        if cached and self._is_cache_valid(cached):
            self._token = cached["access_token"]
            self._expires_at = datetime.fromisoformat(cached["expires_at"])
            logger.info("캐시에서 토큰 로드 성공, 만료: %s", self._expires_at)
            return self._token  # type: ignore[return-value]

        # 신규 발급
        await self._issue_token()
        return self._token  # type: ignore[return-value]

    def get_headers(self, tr_id: str, extra: Optional[dict] = None) -> dict:
        """공통 요청 헤더를 반환한다 (토큰이 이미 로드된 상태여야 함)."""
        if not self._token:
            raise RuntimeError(
                "토큰이 아직 발급되지 않았습니다. get_token()을 먼저 호출하세요."
            )
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if extra:
            headers.update(extra)
        return headers

    # ── 내부 메서드 ─────────────────────────────────────────────────────────

    def _is_token_valid(self) -> bool:
        """메모리에 유효한 토큰이 있는지 확인한다."""
        if not self._token or not self._expires_at:
            return False
        refresh_threshold = self._expires_at - timedelta(
            minutes=TOKEN_REFRESH_MARGIN_MINUTES
        )
        return datetime.now(timezone.utc) < refresh_threshold

    def _is_cache_valid(self, cached: dict) -> bool:
        """캐시 파일의 토큰이 유효한지 확인한다."""
        try:
            expires_at = datetime.fromisoformat(cached["expires_at"])
            refresh_threshold = expires_at - timedelta(
                minutes=TOKEN_REFRESH_MARGIN_MINUTES
            )
            return datetime.now(timezone.utc) < refresh_threshold
        except (KeyError, ValueError):
            return False

    def _load_cache(self) -> Optional[dict]:
        """token_cache.json에서 캐시를 로드한다."""
        try:
            if self.cache_file.exists():
                with self.cache_file.open("r", encoding="utf-8") as f:
                    return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("토큰 캐시 로드 실패: %s", e)
        return None

    def _save_cache(self, access_token: str, expires_at: datetime) -> None:
        """토큰을 token_cache.json에 저장한다."""
        try:
            data = {
                "access_token": access_token,
                "expires_at": expires_at.isoformat(),
            }
            with self.cache_file.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning("토큰 캐시 저장 실패: %s", e)

    async def _issue_token(self) -> None:
        """KIS API에서 신규 토큰을 발급받는다."""
        url = f"{self.base_url}/oauth2/tokenP"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        logger.error(
                            "토큰 발급 실패: HTTP %d, 응답: %s", resp.status, err_text
                        )
                    resp.raise_for_status()
                    data = await resp.json()

            self._token = data["access_token"]
            # KIS는 expires_in(초) 반환
            expires_in_sec = int(data.get("expires_in", 86400))
            self._expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=expires_in_sec
            )
            self._save_cache(self._token, self._expires_at)
            logger.info("토큰 발급 성공, 만료: %s", self._expires_at)

        except aiohttp.ClientResponseError as e:
            if e.status == 403:
                logger.error(
                    "토큰 발급 403 Forbidden 에러! 다음을 확인하세요:\n"
                    "1. 실전/모의투자 키 혼용 여부: 실전투자 키를 .env에 넣고 명령어에 --real을 안 붙이면 기본 모의투자 서버로 접속되어 403이 발생합니다.\n"
                    "   (해결책: 실투자 키라면 `python main.py --real`로 실행)\n"
                    "2. KIS 개발자센터(apiportal.koreainvestment.com)의 IP 등록 여부: 현재 실행 환경의 공인 IP가 등록되어 있는지 확인하세요.\n"
                    "3. APP_KEY 또는 APP_SECRET 오타 여부"
                )
            logger.error("토큰 발급 API 응답 오류: %s", e)
            raise
        except aiohttp.ClientError as e:
            logger.error("토큰 발급 API 네트워크/요청 오류: %s", e)
            raise
        except KeyError as e:
            logger.error("토큰 응답 파싱 오류: %s", e)
            raise
