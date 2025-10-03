# app/wgd_api.py
from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from .settings import SET


class WGDError(Exception):
    """Базовое исключение WGDashboard API."""
    pass


class WGDAPI:
    """
    Клиент для WGDashboard 4.x (и форков).

    Авторизация: заголовок  wg-dashboard-apikey: <API_KEY>

    Частые эндпоинты:
      GET  /api/handshake
      GET  /api/getWireguardConfigurations
      POST /api/addWireguardConfiguration
      POST /api/deleteWireguardConfiguration
      POST /api/addPeers/<configName>            {"peers":[{...}]}
      POST /api/deletePeers/<configName>         {"peers":["<peer_id>", ...]}
      GET  /api/downloadPeer/<configName>?id=<publicKey>   # у некоторых форков id/publicKey/peerId/peer_id
    """

    # ---- параметры по умолчанию / тюнинг ----
    _DEFAULT_TIMEOUT = 20
    _MAX_RETRIES = 2  # доп. попытки на 502/503/504/timeout
    _RETRY_STATUSES = {502, 503, 504}
    _LIMITS = httpx.Limits(max_keepalive_connections=8, max_connections=20)
    _CFG_TTL = 3.0     # секунды кэша для get_configs
    _PEERS_TTL = 3.0   # секунды кэша для get_peers

    def __init__(self, base: Optional[str] = None, api_key: Optional[str] = None, timeout: int = _DEFAULT_TIMEOUT):
        self.base = (str(base or SET.wgd_api_base)).rstrip("/")
        self.api_key = api_key or SET.wgd_api_token
        self.timeout = timeout

        # активное окно "онлайн" (сек), по умолчанию 180
        self.active_window_sec: int = int(getattr(SET, "active_window_sec", 180) or 180)

        # общий httpx-клиент (keep-alive)
        self._client: Optional[httpx.AsyncClient] = None

        # простой in-memory кэш
        # ключ -> (ts, value)
        self._cache: Dict[str, Tuple[float, Any]] = {}

    # ───────────────────────── infra ─────────────────────────

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "wg-dashboard-apikey": self.api_key,
            "content-type": "application/json",
        }

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base}{path}"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                limits=self._LIMITS,
                headers=self.headers,
            )
        return self._client

    async def aclose(self) -> None:
        """Вызывайте при остановке приложения (например, в on_shutdown)."""
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    async def _arequest(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        stream: bool = False,
        expect_json: bool = True,
        retries: int = _MAX_RETRIES,
    ) -> Any:
        """Устойчивый запрос с ретраями на 502/503/504 и таймаутах."""
        cli = await self._get_client()
        url = self._url(path)
        h = dict(self.headers)
        if headers:
            h.update(headers)

        attempt = 0
        last_exc: Optional[Exception] = None
        backoff = 0.25

        while True:
            attempt += 1
            try:
                resp = await cli.request(method, url, params=params, json=json, headers=h)
                if resp.status_code >= 400:
                    # Повторяем попытку только на «временные» коды
                    if attempt <= retries and resp.status_code in self._RETRY_STATUSES:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 2.0)
                        continue
                    # Не светим ключ и хедеры, только метод/путь и код
                    raise WGDError(f"{method} {path} -> {resp.status_code} {resp.text[:400]}")
                if stream:
                    return resp
                if expect_json:
                    # Бывают мусорные ответы — попытка безопасного .json()
                    try:
                        data = resp.json()
                    except Exception:
                        raise WGDError(f"{method} {path} -> invalid JSON")
                    if isinstance(data, dict) and data.get("status") is False:
                        # лаконичная ошибка от API
                        raise WGDError(f"API error: {data.get('message')}")
                    return data
                return resp.text
            except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                last_exc = e
                if attempt <= retries:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 2.0)
                    continue
                raise WGDError(f"{method} {path} -> timeout") from e
            except httpx.HTTPError as e:
                last_exc = e
                if attempt <= retries:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 2.0)
                    continue
                raise WGDError(f"{method} {path} -> {type(e).__name__}") from e

    # ───────────────────────── cache ─────────────────────────

    def _cache_key(self, kind: str, *parts: str) -> str:
        return f"{kind}::" + "::".join(parts)

    def _cache_get(self, key: str, ttl: float) -> Optional[Any]:
        row = self._cache.get(key)
        if not row:
            return None
        ts, val = row
        if (time.time() - ts) <= ttl:
            return val
        self._cache.pop(key, None)
        return None

    def _cache_set(self, key: str, val: Any) -> None:
        self._cache[key] = (time.time(), val)

    # ───────────────────────── base API ─────────────────────────

    async def handshake(self) -> bool:
        data = await self._arequest("GET", "/api/handshake")
        return bool(data and data.get("status"))

    async def get_configs(self, *, force: bool = False) -> List[Dict[str, Any]]:
        """Список конфигураций (кэширование на короткое время)."""
        ck = self._cache_key("configs")
        if not force:
            cached = self._cache_get(ck, self._CFG_TTL)
            if cached is not None:
                return cached
        data = await self._arequest("GET", "/api/getWireguardConfigurations")
        items = data.get("data") or []
        items = [item for item in items if isinstance(item, dict)]
        self._cache_set(ck, items)
        return items

    # ─────────────── configurations (WG instances) ───────────────

    async def add_config(
        self,
        name: str,
        address: str,
        listen_port: int,
        private_key: Optional[str] = None,
        protocol: str = "wg",
    ) -> Dict[str, Any]:
        """Создаёт WireGuard конфигурацию."""
        if private_key is None:
            private_key = base64.b64encode(os.urandom(32)).decode()

        payload = {
            "ConfigurationName": name,
            "Address": address,
            "ListenPort": int(listen_port),
            "PrivateKey": private_key,
            "Protocol": protocol,
        }
        # Сбросим кэш конфигов на успешном ответе
        res = await self._arequest("POST", "/api/addWireguardConfiguration", json=payload)
        self._cache.pop(self._cache_key("configs"), None)
        return res

    async def delete_config(self, name: str) -> Dict[str, Any]:
        payload = {"ConfigurationName": name}
        res = await self._arequest("POST", "/api/deleteWireguardConfiguration", json=payload)
        self._cache.pop(self._cache_key("configs"), None)
        return res

    async def ensure_config(
        self,
        name: str,
        address: str,
        listen_port: int,
        private_key: Optional[str] = None,
        protocol: str = "wg",
    ) -> None:
        """Идемпотентно гарантирует наличие конфигурации `name`."""
        try:
            existing = {self._cfg_name(c) for c in await self.get_configs()}
        except Exception:
            existing = set()
        if name in existing:
            return

        try:
            await self.add_config(name, address, listen_port, private_key=private_key, protocol=protocol)
        except WGDError as e:
            if "already" in str(e).lower():
                return
            raise

    # ───────────────────────── peers ─────────────────────────

    async def add_peers(self, config: str, peers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        path = f"/api/addPeers/{quote(config)}"
        payload = {"peers": peers}
        data = await self._arequest("POST", path, json=payload)
        # Сбросим кэш пиров для данного конфига
        self._cache.pop(self._cache_key("peers", config), None)
        return data.get("data") or []

    async def add_peer_minimal(
        self,
        config: str,
        *,
        name: str = "",
        allowed_ip: Optional[str] = None,
        keepalive: int = 21,
        dns: str = "1.1.1.1",
    ) -> Dict[str, Any]:
        """Создать одного пира по минимально необходимым полям."""
        peer_body: Dict[str, Any] = {"name": name}
        if allowed_ip:
            peer_body.update({"allowed_ip": allowed_ip, "keepalive": keepalive, "DNS": dns})

        try:
            created = await self.add_peers(config, [peer_body])
            if created:
                return created[0]
        except WGDError as e:
            # если это не про allowed_ip — пробрасываем
            if "allowed_ip" not in str(e).lower():
                raise

        # Подберём allowed_ip автоматически
        next_ip = await self._suggest_next_allowed_ip(config)
        peer_body = {"name": name, "allowed_ip": next_ip, "keepalive": keepalive, "DNS": dns}
        created = await self.add_peers(config, [peer_body])
        if not created:
            raise WGDError("Peer was not created by API (empty 'data').")
        return created[0]

    async def delete_peers(self, config: str, peer_ids: List[str]) -> Dict[str, Any]:
        path = f"/api/deletePeers/{quote(config)}"
        payload = {"peers": peer_ids}
        res = await self._arequest("POST", path, json=payload)
        self._cache.pop(self._cache_key("peers", config), None)
        return res

    async def get_peers(self, config: str, *, force: bool = False) -> List[Dict[str, Any]]:
        """
        Возвращает список пиров для конфигурации, пробуя набор известных путей разных форков.
        Результат кэшируется на короткое время.
        """
        cfg = str(config)
        ck = self._cache_key("peers", cfg)
        if not force:
            cached = self._cache_get(ck, self._PEERS_TTL)
            if cached is not None:
                return cached

        esc = quote(cfg)
        attempts = [
            ("GET", f"/api/getPeers/{esc}", None, None),
            ("GET", f"/api/getPeersList/{esc}", None, None),
            ("GET", f"/api/getWireguardConfiguration/{esc}", None, None),
            ("GET", f"/api/getConfiguration/{esc}", None, None),
            ("GET", f"/api/getConfigurationPeers/{esc}", None, None),
        ]
        peers: List[Dict[str, Any]] = []
        for method, path, params, body in attempts:
            try:
                data = await self._arequest(method, path, params=params, json=body)
                if isinstance(data, dict):
                    # тип 1: {"status":true,"data":[peer,...]}
                    if isinstance(data.get("data"), list):
                        peers = data["data"]
                        break
                    # тип 2: {"status":true,"data":{"Peers":[peer,...]}}
                    if isinstance(data.get("data"), dict):
                        bucket = data["data"]
                        got = bucket.get("Peers") or bucket.get("peers") or []
                        if isinstance(got, list):
                            peers = got
                            break
            except WGDError:
                continue
            except Exception:
                continue

        if not isinstance(peers, list):
            peers = []

        self._cache_set(ck, peers)
        return peers

    # ───────────────────────── parse helpers ─────────────────────────

    def _cfg_name(self, cfg: Dict[str, Any]) -> Optional[str]:
        return cfg.get("Name") or cfg.get("name")

    def _cfg_peers(self, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        peers = cfg.get("Peers")
        if not isinstance(peers, list):
            peers = cfg.get("peers")
        return peers if isinstance(peers, list) else []

    def _cfg_address(self, cfg: Dict[str, Any]) -> Optional[str]:
        for k in ("Address", "address", "AddressIPv4", "AddressIpv4"):
            v = cfg.get(k)
            if isinstance(v, str) and "/" in v:
                return v
        return None

    def _peer_id(self, peer: Dict[str, Any]) -> Optional[str]:
        for key in ("id", "peer_id", "Id"):
            if key in peer and peer[key] is not None:
                return str(peer[key])
        return None

    def _peer_public_key(self, peer: Dict[str, Any]) -> Optional[str]:
        for key in ("publicKey", "public_key", "PublicKey"):
            if key in peer and peer[key]:
                return str(peer[key])
        return None

    def _peer_allowed_ip(self, peer: Dict[str, Any]) -> Optional[str]:
        return peer.get("allowed_ip") or peer.get("AllowedIP") or peer.get("AllowedIp")

    def _peer_handshake_ts(self, peer: Dict[str, Any]) -> Optional[int]:
        for k in ("LatestHandshake", "latestHandshake", "latest_handshake",
                  "LastHandshake", "lastHandshake", "Handshake", "handshake"):
            ts = self._to_unix(peer.get(k))
            if ts is not None:
                return ts
        return None

    def _peer_transfer_pair(self, peer: Dict[str, Any]) -> Tuple[int, int]:
        candidates = [
            ("rx", "tx"),
            ("Rx", "Tx"),
            ("receive", "sent"),
            ("download", "upload"),
            ("transferRx", "transferTx"),
            ("TransferRx", "TransferTx"),
            ("ReceiveBytes", "TransmitBytes"),
        ]
        for rxk, txk in candidates:
            rx = peer.get(rxk)
            tx = peer.get(txk)
            if rx is not None or tx is not None:
                return self._num(rx), self._num(tx)
        return 0, 0

    async def _find_peer_info(self, peer_id_or_key: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Вернёт (config_name, peer_dict); ищет по id или publicKey во всех конфигах (с догрузкой пиров)."""
        needle = str(peer_id_or_key)
        for cfg in await self.get_configs():
            name = self._cfg_name(cfg)
            if not name:
                continue
            peers = self._cfg_peers(cfg)
            if not peers:
                peers = await self.get_peers(name)
            for p in peers:
                if self._peer_id(p) == needle or self._peer_public_key(p) == needle:
                    return name, p
        return None

    async def _suggest_next_allowed_ip(self, config: str) -> str:
        """
        Выбирает следующий свободный /32 адрес в подсети конфигурации.
        Если не удаётся — возвращает 10.66.66.2/32.
        """
        cfgs = await self.get_configs()
        target = next((c for c in cfgs if self._cfg_name(c) == config), None)

        network: Optional[ipaddress.IPv4Network] = None
        if target:
            addr_str = self._cfg_address(target)
            if addr_str:
                try:
                    iface = ipaddress.ip_interface(addr_str)
                    network = iface.network
                except Exception:
                    network = None

        used: set[int] = set()
        raw_peers = self._cfg_peers(target or {}) or await self.get_peers(config)
        for p in raw_peers:
            ip_cidr = self._peer_allowed_ip(p)
            if not ip_cidr or "/" not in ip_cidr:
                continue
            ip_str = ip_cidr.split("/", 1)[0]
            try:
                ip = ipaddress.IPv4Address(ip_str)
                used.add(int(ip))
            except Exception:
                continue

        # если знаем подсеть — идём по hosts(); считаем, что .1 занят сервером
        if network:
            for ip in network.hosts():
                last = ip.packed[-1]
                if last in (0, 1):
                    continue
                if int(ip) not in used:
                    return f"{ip}/32"

        # Fallback — инкремент последнего
        if not used:
            return "10.66.66.2/32"
        current = max(used) + 1
        if ipaddress.IPv4Address(current).packed[-1] in (0, 1):
            current += 1
        return f"{ipaddress.IPv4Address(current)}/32"

    # ───────────────────────── download ─────────────────────────

    async def _try_download(self, method: str, path: str, *, params=None, json=None):
        """
        Универсальный загрузчик: понимает и octet-stream, и JSON с data.file/fileName.
        Возвращает (filename, content_bytes) или None.
        """
        cli = await self._get_client()
        headers = {**self.headers, "accept": "*/*"}
        url = self._url(path)

        try:
            r = await cli.request(method, url, params=params, json=json, headers=headers)
        except httpx.HTTPError as e:
            return None

        if r.status_code != 200:
            return None

        ctype = (r.headers.get("content-type") or r.headers.get("Content-Type") or "").lower()
        ctype = ctype.split(";", 1)[0].strip()

        # JSON: {"status":true,"data":{"file":"...","fileName":"..."}}
        if "application/json" in ctype or (r.text and r.text[:1] in "{["):
            try:
                data = r.json()
            except Exception:
                text = r.text
                filename = "peer.conf"
                return filename, text.encode("utf-8", errors="replace")

            payload = data.get("data") or {}
            text = payload.get("file") or data.get("file")
            filename = payload.get("fileName") or data.get("fileName") or "peer.conf"

            if not filename.endswith(".conf"):
                filename = f"{filename}.conf"
            if not isinstance(text, str) or "[Interface]" not in text:
                raise WGDError("downloadPeer returned JSON без корректного 'data.file'.")
            return filename, text.encode("utf-8")

        # Бинарь/текст напрямую
        filename = "peer.conf"
        cd = r.headers.get("content-disposition") or r.headers.get("Content-Disposition")
        if cd and "filename=" in cd:
            filename = cd.split("filename=")[-1].strip().strip('"')
        if not filename.endswith(".conf"):
            filename = f"{filename}.conf"
        return filename, r.content

    async def download_peer_conf(self, config: str, public_key: str) -> Tuple[str, bytes]:
        """Для ряда форков: GET /api/downloadPeer/<config>?id=<publicKey>."""
        path = f"/api/downloadPeer/{quote(config)}"

        # основной (самый частый) вариант
        res = await self._try_download("GET", path, params={"id": str(public_key)})
        if res:
            return res

        # запасные названия параметров + иногда POST
        attempts = [
            ("GET", path, {"peer_id": public_key}, None),
            ("GET", path, {"peerId": public_key}, None),
            ("GET", path, {"publicKey": public_key}, None),
            ("GET", path, {"public_key": public_key}, None),
            ("POST", path, {"id": public_key}, {}),
        ]
        last: List[str] = []
        for method, p, params, body in attempts:
            got = await self._try_download(method, p, params=params, json=body)
            if got:
                return got
            try:
                cli = await self._get_client()
                r = await cli.request(method, self._url(p), params=params, json=body, headers={**self.headers, "accept": "*/*"})
                last.append(f"{method} {p} -> {r.status_code}")
            except Exception as e:
                last.append(f"{method} {p} -> {type(e).__name__}: {e}")
        raise WGDError("downloadPeer failed. Tried variants: " + "; ".join(last))

    # ───────────────────────── adapters for handlers ─────────────────────────

    async def create_peer(self, config: str, name: str, *, allowed_ip: Optional[str] = None) -> str:
        """Создаёт пира и возвращает publicKey (стабильный идентификатор)."""
        created = await self.add_peer_minimal(config, name=name, allowed_ip=allowed_ip)
        pk = self._peer_public_key(created)
        if not pk:
            pid = self._peer_id(created)
            if not pid:
                raise WGDError("API returned peer object without id/publicKey")
            return str(pid)
        return str(pk)

    async def get_peer_config(self, arg1: str, arg2: Optional[str] = None) -> str:
        """
        - get_peer_config(config, public_key)
        - get_peer_config(peer_id_or_public_key)
        """
        if arg2 is not None:
            cfg_name = arg1
            public_key = arg2
            _, content = await self.download_peer_conf(cfg_name, public_key)
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                return content.decode("latin-1", errors="replace")

        needle = str(arg1)
        found = await self._find_peer_info(needle)
        if not found:
            # не нашли по id — предположим, что нам дали publicKey и используем дефолтный интерфейс
            cfg_name = SET.wgd_interface
            _, content = await self.download_peer_conf(cfg_name, needle)
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                return content.decode("latin-1", errors="replace")

        cfg_name, peer = found
        public_key = self._peer_public_key(peer)
        if not public_key:
            attempts = [("peer_id", needle), ("peerId", needle), ("publicKey", needle), ("public_key", needle)]
            last = []
            for k, v in attempts:
                try:
                    _, content = await self.download_peer_conf(cfg_name, v)
                    return content.decode("utf-8")
                except Exception as e:
                    last.append(f"{k}: {type(e).__name__}")
            raise WGDError("Peer found, but publicKey missing. Tried legacy params: " + ", ".join(last))

        _, content = await self.download_peer_conf(cfg_name, public_key)
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return content.decode("latin-1", errors="replace")

    async def delete_peer(self, arg1: str, arg2: Optional[str] = None) -> bool:
        """
        - delete_peer(config, peer_id_or_public_key)
        - delete_peer(peer_id_or_public_key)
        """
        if arg2 is not None:
            await self.delete_peers(str(arg1), [str(arg2)])
            return True
        pid = str(arg1)
        found = await self._find_peer_info(pid)
        cfg = found[0] if found else SET.wgd_interface
        await self.delete_peers(cfg, [pid])
        return True

    # ───────────────────────── admin helpers / stats ─────────────────────────

    async def list_config_names(self) -> List[str]:
        return [self._cfg_name(c) for c in await self.get_configs() if self._cfg_name(c)]

    async def get_config_with_peers(self, name: str) -> Dict[str, Any]:
        """Возвращает объект конфига с полноценным списком пиров (c догрузкой при необходимости)."""
        name = str(name)
        configs = await self.get_configs()
        for cfg in configs:
            if self._cfg_name(cfg) != name:
                continue
            peers = self._cfg_peers(cfg)
            if not peers:
                peers = await self.get_peers(name)
            # сохранить, чтобы дальше не дёргать сеть
            if isinstance(cfg, dict):
                if "Peers" in cfg:
                    cfg["Peers"] = peers
                elif "peers" in cfg:
                    cfg["peers"] = peers
                else:
                    cfg["Peers"] = peers
            return cfg
        raise WGDError(f"Config '{name}' not found")

    async def peers_of(self, name: str) -> List[Dict[str, Any]]:
        cfg = await self.get_config_with_peers(name)
        peers = self._cfg_peers(cfg)
        if not peers:
            peers = await self.get_peers(name)
        return peers

    async def all_configs_with_counts(self) -> List[Dict[str, Any]]:
        out = []
        for cfg in await self.get_configs():
            nm = self._cfg_name(cfg)
            if not nm:
                continue
            peers = self._cfg_peers(cfg)
            if not peers:
                peers = await self.get_peers(nm)
            out.append({"name": nm, "peers_count": len(peers)})
        return out

    async def aggregate_stats(self) -> Dict[str, Any]:
        """Агрегированная статистика по всем конфигам."""
        now = int(time.time())
        online_window = int(self.active_window_sec)
        total_cfg = 0
        total_peers = 0
        online = 0
        offline = 0
        sum_rx = 0
        sum_tx = 0

        for cfg in await self.get_configs():
            total_cfg += 1
            peers = self._cfg_peers(cfg)
            if not peers:
                peers = await self.get_peers(self._cfg_name(cfg) or "")
            for p in peers:
                total_peers += 1
                hs = self._peer_handshake_ts(p) or 0
                if now - hs <= online_window:
                    online += 1
                else:
                    offline += 1
                rx, tx = self._peer_transfer_pair(p)
                sum_rx += rx
                sum_tx += tx

        return {
            "configs": total_cfg,
            "peers": total_peers,
            "online": online,
            "offline": offline,
            "rx": sum_rx,
            "tx": sum_tx,
        }

    # ───────────────────────── normalization & snapshot ─────────────────────────

    def _num(self, v) -> int:
        """
        Перевод произвольного значения (в т.ч. строки с единицами вида '0.12 GB') в байты/целое.
        """
        try:
            if v is None:
                return 0
            if isinstance(v, (int, float)):
                return int(float(v))
            s = str(v).strip()
            if not s:
                return 0
            # поддержка строк вида "0.0032 GB"
            parts = s.split()
            # если это просто число в строке
            try:
                if len(parts) == 1:
                    return int(float(parts[0]))
            except Exception:
                pass
            # число + единица
            try:
                val = float(parts[0])
            except Exception:
                return 0
            mult = 1
            if len(parts) > 1:
                unit = parts[1].upper()
                order = {"B": 0, "KB": 1, "MB": 2, "GB": 3, "TB": 4}.get(unit, 0)
                mult = 1024 ** order
            return int(val * mult)
        except Exception:
            return 0

    def _to_unix(self, v) -> Optional[int]:
        """
        Нормализация времени к UNIX-таймстампу.
        Поддержка: секунды/мс/нс (int/float), ISO-строки, относительные строки "8s","3m","2h","1d".
        """
        try:
            if v is None:
                return None
            if isinstance(v, (int, float)):
                x = float(v)
                if x > 1e12:       # наносекунды
                    x = x / 1e9
                elif x > 1e10:     # миллисекунды
                    x = x / 1e3
                return int(x)
            s = str(v).strip()
            if not s:
                return None
            # относительные значения
            if s.endswith(("s", "m", "h", "d")) and len(s) > 1:
                now = int(time.time())
                try:
                    num = float(s[:-1])
                except Exception:
                    return None
                t = s[-1]
                if t == "s":
                    return now - int(num)
                if t == "m":
                    return now - int(num * 60)
                if t == "h":
                    return now - int(num * 3600)
                if t == "d":
                    return now - int(num * 86400)
            # ISO-строка
            s2 = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s2)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            return None

    def _peer_rx(self, p: Dict[str, Any]) -> int:
        for k in ("transferRx", "TransferRx", "rx", "Rx", "receive", "ReceiveBytes"):
            if k in p:
                return self._num(p[k])
        return 0

    def _peer_tx(self, p: Dict[str, Any]) -> int:
        for k in ("transferTx", "TransferTx", "tx", "Tx", "sent", "TransmitBytes"):
            if k in p:
                return self._num(p[k])
        return 0

    def _peer_last_hs(self, p: Dict[str, Any]) -> Optional[int]:
        for k in ("LatestHandshake", "latestHandshake", "latest_handshake", "LastHandshake"):
            if k in p and p[k] is not None:
                return self._to_unix(p[k])
        # запасной путь
        return self._peer_handshake_ts(p)

    def _peer_name(self, p: Dict[str, Any]) -> str:
        pk = self._peer_public_key(p) or ""
        return p.get("name") or p.get("Name") or (pk[-8:] if pk else "(no-name)")

    def _norm_peer(self, cfg_name: str, p: Dict[str, Any]) -> Dict[str, Any]:
        pid = self._peer_id(p) or self._peer_public_key(p) or ""
        pk = self._peer_public_key(p) or ""
        name = self._peer_name(p)
        allowed_ip = self._peer_allowed_ip(p) or ""
        rx = self._peer_rx(p)
        tx = self._peer_tx(p)
        hs = self._peer_last_hs(p)
        try:
            active = (hs is not None) and (time.time() - hs <= self.active_window_sec)
        except Exception:
            active = False
        return {
            "config": cfg_name,
            "id": str(pid),
            "public_key": pk,
            "name": name,
            "allowed_ip": allowed_ip,
            "rx": rx,
            "tx": tx,
            "last_handshake": hs,
            "active": active,
            "raw": p,
        }

    async def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Снимок всех конфигураций и нормализованных пиров."""
        snap: Dict[str, Dict[str, Any]] = {}
        for cfg in await self.get_configs():
            name = self._cfg_name(cfg)
            if not name:
                continue
            raw_peers = self._cfg_peers(cfg)
            if not raw_peers:
                raw_peers = await self.get_peers(name)
            peers = [self._norm_peer(name, p) for p in raw_peers]
            snap[name] = {"raw": cfg, "peers": peers}
        return snap

    async def totals(self) -> Dict[str, Any]:
        snap = await self.snapshot()
        cfgs = len(snap)
        peers = sum(len(v["peers"]) for v in snap.values())
        active = sum(1 for v in snap.values() for p in v["peers"] if p["active"])
        rx = sum(p["rx"] for v in snap.values() for p in v["peers"])
        tx = sum(p["tx"] for v in snap.values() for p in v["peers"])
        return {"configs": cfgs, "peers": peers, "active_peers": active, "rx": rx, "tx": tx}

    def find_peer_in_snapshot(
        self,
        snap: Dict[str, Dict[str, Any]],
        cfg_name: str,
        peer_id: str
    ) -> Optional[Dict[str, Any]]:
        """Быстрый поиск нормализованного пира в снапшоте (по id или public_key)."""
        bucket = snap.get(cfg_name) or {}
        for p in bucket.get("peers", []):
            if p["id"] == str(peer_id) or (p["public_key"] and p["public_key"] == str(peer_id)):
                return p
        return None


# Экземпляр по умолчанию
wgd = WGDAPI()
