# app/wgd_api.py
from __future__ import annotations

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
    """Общее исключение WGDashboard API."""
    pass


class WGDAPI:
    """
    Клиент WGDashboard 4.x

    Авторизация: заголовок  wg-dashboard-apikey: <API_KEY>

    Встречающиеся эндпоинты:
      GET  /api/handshake
      GET  /api/getWireguardConfigurations
      POST /api/addWireguardConfiguration                 # создать конфигурацию
      POST /api/deleteWireguardConfiguration              # удалить конфигурацию
      POST /api/addPeers/<configName>            {"peers":[{...}]}
      POST /api/deletePeers/<configName>         {"peers": ["<peer_id>", ...]}
      GET  /api/downloadPeer/<configName>?id=<publicKey>  # у вас используется именно ?id
      # на других сборках: ?peer_id / ?peerId / ?publicKey и пр.
    """

    def __init__(self, base: Optional[str] = None, api_key: Optional[str] = None, timeout: int = 20):
        self.base = (str(base or SET.wgd_api_base)).rstrip("/")
        self.api_key = api_key or SET.wgd_api_token
        self.timeout = timeout

    # -------------- низкоуровневые примитивы --------------

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
    ) -> Any:
        h = dict(self.headers)
        if headers:
            h.update(headers)

        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            resp = await cli.request(method, self._url(path), params=params, json=json, headers=h)
            if resp.status_code >= 400:
                raise WGDError(f"{method} {self._url(path)} -> {resp.status_code} {resp.text[:400]}")
            if stream:
                return resp
            if expect_json:
                data = resp.json()
                if isinstance(data, dict) and data.get("status") is False:
                    raise WGDError(f"API error: {data.get('message')}")
                return data
            return resp.text

    # -------------- базовые операции API --------------

    async def handshake(self) -> bool:
        data = await self._arequest("GET", "/api/handshake")
        return bool(data and data.get("status"))

    async def get_configs(self) -> List[Dict[str, Any]]:
        data = await self._arequest("GET", "/api/getWireguardConfigurations")
        items = data.get("data") or []
        return [item for item in items if isinstance(item, dict)]

    # ---- Конфигурации (WG instances) ----

    async def add_config(
        self,
        name: str,
        address: str,
        listen_port: int,
        private_key: Optional[str] = None,
        protocol: str = "wg",
    ) -> Dict[str, Any]:
        """
        Создаёт WireGuard конфигурацию.
        address: например '10.0.42.1/24'
        protocol: 'wg' (или 'awg' для AmneziaWG)
        """
        if private_key is None:
            # WireGuard private key — 32 случайных байта в base64
            private_key = base64.b64encode(os.urandom(32)).decode()

        payload = {
            "ConfigurationName": name,
            "Address": address,
            "ListenPort": int(listen_port),
            "PrivateKey": private_key,
            "Protocol": protocol,
        }
        return await self._arequest("POST", "/api/addWireguardConfiguration", json=payload)

    async def delete_config(self, name: str) -> Dict[str, Any]:
        payload = {"ConfigurationName": name}
        return await self._arequest("POST", "/api/deleteWireguardConfiguration", json=payload)

    async def ensure_config(
        self,
        name: str,
        address: str,
        listen_port: int,
        private_key: Optional[str] = None,
        protocol: str = "wg",
    ) -> None:
        """
        Идемпотентно гарантирует наличие конфигурации.
        Если уже существует — просто выходим.
        """
        try:
            existing = {self._cfg_name(c) for c in await self.get_configs()}
        except Exception:
            existing = set()
        if name in existing:
            return

        try:
            await self.add_config(name, address, listen_port, private_key=private_key, protocol=protocol)
        except WGDError as e:
            msg = str(e)
            if "already" in msg.lower():
                # некоторые форки возвращают status=false с сообщением "Already have a configuration..."
                return
            raise

    # ---- Пиры ----

    async def add_peers(self, config: str, peers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        path = f"/api/addPeers/{quote(config)}"
        payload = {"peers": peers}
        data = await self._arequest("POST", path, json=payload)
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
        """
        Создать одного пира по минимально необходимым полям.
        Возвращает объект пира из ответа API.
        """
        # 1-я попытка — по полям, которые ваша сборка точно понимает
        peer_body: Dict[str, Any] = {"name": name}
        if allowed_ip:
            peer_body.update({"allowed_ip": allowed_ip, "keepalive": keepalive, "DNS": dns})

        try:
            created = await self.add_peers(config, [peer_body])
            if created:
                return created[0]
        except WGDError as e:
            # если ошибка явно не про allowed_ip — пробрасываем
            if "allowed_ip" not in str(e).lower():
                raise

        # 2-я попытка — подберём allowed_ip автоматически
        next_ip = await self._suggest_next_allowed_ip(config)
        peer_body = {"name": name, "allowed_ip": next_ip, "keepalive": keepalive, "DNS": dns}
        created = await self.add_peers(config, [peer_body])
        if not created:
            raise WGDError("Peer was not created by API (empty 'data').")
        return created[0]

    async def delete_peers(self, config: str, peer_ids: List[str]) -> Dict[str, Any]:
        path = f"/api/deletePeers/{quote(config)}"
        payload = {"peers": peer_ids}
        return await self._arequest("POST", path, json=payload)

    # -------------- утилиты разбора структур --------------

    def _cfg_name(self, cfg: Dict[str, Any]) -> Optional[str]:
        return cfg.get("Name") or cfg.get("name")

    def _cfg_peers(self, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        peers = cfg.get("Peers")
        if not isinstance(peers, list):
            peers = cfg.get("peers")
        return peers if isinstance(peers, list) else []

    def _cfg_address(self, cfg: Dict[str, Any]) -> Optional[str]:
        # пытаемся достать серверный адрес/префикс конфигурации
        for k in ("Address", "address", "AddressIPv4", "AddressIpv4"):
            v = cfg.get(k)
            if isinstance(v, str) and "/" in v:
                return v
        return None

    def _peer_id(self, peer: Dict[str, Any]) -> Optional[str]:
        # разные реализации: id / peer_id / Id
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
        # разные варианты ключей в форках:
        for k in ("LatestHandshake", "latestHandshake", "latest_handshake",
                  "LastHandshake", "lastHandshake", "Handshake", "handshake"):
            v = peer.get(k)
            ts = self._to_unix(v)
            if ts is not None:
                return ts
        return None

    def _peer_transfer_pair(self, peer: Dict[str, Any]) -> Tuple[int, int]:
        # ищем rx/tx в разных стилях
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
        """
        Вернёт (config_name, peer_dict) для пира, ищет по id ИЛИ по publicKey.
        """
        needle = str(peer_id_or_key)
        configs = await self.get_configs()
        for cfg in configs:
            name = self._cfg_name(cfg)
            if not name:
                continue
            for p in self._cfg_peers(cfg):
                pid = self._peer_id(p)
                pk = self._peer_public_key(p)
                if pid == needle or pk == needle:
                    return name, p
        return None

    async def _suggest_next_allowed_ip(self, config: str) -> str:
        """
        Выбирает следующий свободный /32 адрес в подсети конфигурации.
        Если не удаётся — падает назад на 10.66.66.2/32.
        """
        # попытаемся понять подсеть из самого конфига
        cfgs = await self.get_configs()
        target = next((c for c in cfgs if self._cfg_name(c) == config), None)

        network: Optional[ipaddress.IPv4Network] = None
        if target:
            addr_str = self._cfg_address(target)  # например "10.0.20.1/24"
            if addr_str:
                try:
                    iface = ipaddress.ip_interface(addr_str)
                    network = iface.network
                except Exception:
                    network = None

        used: set[int] = set()
        if target:
            for p in self._cfg_peers(target):
                ip_cidr = self._peer_allowed_ip(p)
                if not ip_cidr or "/" not in ip_cidr:
                    continue
                ip_str = ip_cidr.split("/", 1)[0]
                try:
                    ip = ipaddress.IPv4Address(ip_str)
                except Exception:
                    continue
                used.add(int(ip))

        # если знаем подсеть — пробуем пройтись по host'ам
        if network:
            # считаем, что .1 занят сервером, начнем с .2
            for ip in network.hosts():
                if int(ip) not in used and ip.packed[-1] not in (0, 1):
                    return f"{ip}/32"

        # Fallback — старое поведение
        if not used:
            return "10.66.66.2/32"

        current = max(used) + 1
        if ipaddress.IPv4Address(current).packed[-1] in (0, 1):
            current += 1
        return f"{ipaddress.IPv4Address(current)}/32"

    # -------------- скачивание конфигов --------------

    async def _try_download(self, method: str, path: str, *, params=None, json=None):
        """
        Универсальный загрузчик: понимает и application/octet-stream,
        и JSON с data.file/fileName.
        Возвращает (filename, content_bytes) или None, если статус != 200.
        """
        headers = {**self.headers, "accept": "*/*"}
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            r = await cli.request(method, self._url(path), params=params, json=json, headers=headers)
            if r.status_code != 200:
                return None

            ctype = (r.headers.get("content-type") or r.headers.get("Content-Type") or "").lower()
            ctype = ctype.split(";", 1)[0].strip()

            # Вариант 1: сервер отдаёт JSON: {"status":true,"data":{"file":"...","fileName":"..."}}
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
                    raise WGDError("downloadPeer returned JSON без корректного 'data.file' с конфигом.")

                return filename, text.encode("utf-8")

            # Вариант 2: бинарь/текст напрямую
            filename = "peer.conf"
            cd = r.headers.get("content-disposition") or r.headers.get("Content-Disposition")
            if cd and "filename=" in cd:
                filename = cd.split("filename=")[-1].strip().strip('"')
            if not filename.endswith(".conf"):
                filename = f"{filename}.conf"

            return filename, r.content

    async def download_peer_conf(self, config: str, public_key: str) -> Tuple[str, bytes]:
        """
        Для вашей сборки: GET /api/downloadPeer/<config>?id=<publicKey>
        """
        path = f"/api/downloadPeer/{quote(config)}"

        # основной (верный для вашего сервера) вариант
        res = await self._try_download("GET", path, params={"id": str(public_key)})
        if res:
            return res

        # запасные варианты на случай иного форка
        attempts = [
            ("GET", path, {"peer_id": public_key}, None),
            ("GET", path, {"peerId": public_key}, None),
            ("GET", path, {"publicKey": public_key}, None),
            ("GET", path, {"public_key": public_key}, None),
            ("POST", path, {"id": public_key}, {}),  # иногда встречается POST
        ]
        last: List[str] = []
        for method, p, params, body in attempts:
            got = await self._try_download(method, p, params=params, json=body)
            if got:
                return got
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as cli:
                    r = await cli.request(
                        method, self._url(p), params=params, json=body, headers={**self.headers, "accept": "*/*"}
                    )
                    last.append(f"{method} {p} -> {r.status_code}")
            except Exception as e:
                last.append(f"{method} {p} -> {type(e).__name__}: {e}")
        raise WGDError("downloadPeer failed. Tried variants: " + "; ".join(last))

    # -------------- адаптеры для хэндлеров --------------

    async def create_peer(self, config: str, name: str, *, allowed_ip: Optional[str] = None) -> str:
        """
        Создаёт пира в конфигурации `config`.
        Возвращает publicKey пира (его удобно использовать как устойчивый идентификатор).
        """
        created = await self.add_peer_minimal(config, name=name, allowed_ip=allowed_ip)
        pk = self._peer_public_key(created)
        if not pk:
            # в крайнем случае попробуем id — но лучше хранить publicKey
            pid = self._peer_id(created)
            if not pid:
                raise WGDError("API returned peer object without id/publicKey")
            return str(pid)
        return str(pk)

    async def get_peer_config(self, arg1: str, arg2: Optional[str] = None) -> str:
        """
        Универсальный метод:
          - если вызван как get_peer_config(config, public_key) — качаем по паре (cfg, pk)
          - если вызван как get_peer_config(peer_id_or_public_key) — сначала ищем пира и его конфиг
        Возвращает текст .conf
        """
        # Вариант A: переданы (config, public_key)
        if arg2 is not None:
            cfg_name = arg1
            public_key = arg2
            _, content = await self.download_peer_conf(cfg_name, public_key)
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                return content.decode("latin-1", errors="replace")

        # Вариант B: передан только peer_id/public_key — найдём конфиг по каталогу
        needle = str(arg1)
        found = await self._find_peer_info(needle)
        if not found:
            # если не нашли по id — попробуем считать, что нам передали уже publicKey и использовать дефолтный интерфейс
            cfg_name = SET.wgd_interface
            _, content = await self.download_peer_conf(cfg_name, needle)
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                return content.decode("latin-1", errors="replace")

        cfg_name, peer = found
        public_key = self._peer_public_key(peer)
        if not public_key:
            # совсем крайний случай — попробуем различными параметрами
            attempts = [("peer_id", needle), ("peerId", needle), ("publicKey", needle), ("public_key", needle)]
            last = []
            for k, v in attempts:
                try:
                    _, content = await self.download_peer_conf(cfg_name, v)  # вдруг сервер примет это как id
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
        Универсальный метод:
          - delete_peer(config, peer_id_or_public_key)
          - delete_peer(peer_id_or_public_key)           # совместимость со старым кодом
        """
        # Новый формат: (config, peer_id)
        if arg2 is not None:
            cfg = str(arg1)
            pid = str(arg2)
            await self.delete_peers(cfg, [pid])
            return True

        # Старый формат: (peer_id) — найдём конфиг по каталогу
        pid = str(arg1)
        found = await self._find_peer_info(pid)
        cfg = found[0] if found else SET.wgd_interface
        await self.delete_peers(cfg, [pid])
        return True

    # ---------- Admin helpers (для обзора всех конфигов/пиров) ----------

    async def list_config_names(self) -> List[str]:
        return [self._cfg_name(c) for c in await self.get_configs() if self._cfg_name(c)]

    async def get_config_with_peers(self, name: str) -> Dict[str, Any]:
        """
        Возвращает объект конфига с полем Peers/peers.
        Если в общей выдаче Peers пустые — пробуем детальные эндпоинты.
        """
        name = str(name)
        configs = await self.get_configs()
        for cfg in configs:
            if self._cfg_name(cfg) == name and self._cfg_peers(cfg):
                return cfg

        # пробуем детальные пути (в разных форках по-разному называются)
        attempts = [
            f"/api/getWireguardConfiguration/{quote(name)}",
            f"/api/getWireguardConfig/{quote(name)}",
            f"/api/getWGConfiguration/{quote(name)}",
        ]
        last_err = None
        for path in attempts:
            try:
                data = await self._arequest("GET", path)
                obj = data.get("data") if isinstance(data, dict) else None
                if isinstance(obj, dict) and self._cfg_peers(obj) is not None:
                    return obj
            except Exception as e:
                last_err = e
        # вернем хотя бы оболочку
        for cfg in configs:
            if self._cfg_name(cfg) == name:
                return cfg
        raise WGDError(f"Config '{name}' not found: {last_err or 'no data'}")

    async def peers_of(self, name: str) -> List[Dict[str, Any]]:
        cfg = await self.get_config_with_peers(name)
        return self._cfg_peers(cfg)

    async def all_configs_with_counts(self) -> List[Dict[str, Any]]:
        out = []
        for cfg in await self.get_configs():
            nm = self._cfg_name(cfg)
            if not nm:
                continue
            peers = self._cfg_peers(cfg)
            out.append({"name": nm, "peers_count": len(peers or [])})
        return out

    async def aggregate_stats(self) -> Dict[str, Any]:
        """
        Суммарная статистика по всем конфигам: количество конфигов/пиров, онлайн/оффлайн, трафик.
        Онлайн считаем по рукопожатию < 180 секунд.
        """
        now = int(time.time())
        online_window = 180

        total_cfg = 0
        total_peers = 0
        online = 0
        offline = 0
        sum_rx = 0
        sum_tx = 0

        for cfg in await self.get_configs():
            total_cfg += 1
            for p in self._cfg_peers(cfg):
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

    # ---------- Normalization & snapshots ----------

    def _num(self, v) -> int:
        try:
            if v is None:
                return 0
            if isinstance(v, (int, float)):
                return int(v)
            s = str(v).strip()
            if not s:
                return 0
            return int(float(s))
        except Exception:
            return 0

    def _to_unix(self, v) -> Optional[int]:
        # Вход может быть в секундах/мс/строке/ISO/None
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
            # просто число в строке?
            try:
                x = float(s)
                if x > 1e12:
                    x = x / 1e9
                elif x > 1e10:
                    x = x / 1e3
                return int(x)
            except Exception:
                pass
            # ISO-строка
            s2 = s.replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(s2)
            except Exception:
                return None
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
        active = False
        try:
            active = (hs is not None) and (time.time() - hs <= 130)
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
        """
        Снимок всех конфигураций и нормализованных пиров.
        { cfg_name: {"raw": <cfg>, "peers":[norm_peer,...]} }
        """
        snap: Dict[str, Dict[str, Any]] = {}
        for cfg in await self.get_configs():
            name = self._cfg_name(cfg)
            if not name:
                continue
            peers = [self._norm_peer(name, p) for p in self._cfg_peers(cfg)]
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
        """
        Быстрый поиск нормализованного пира в уже полученном снапшоте.
        Сравниваем и по id, и по public_key.
        """
        bucket = snap.get(cfg_name) or {}
        for p in bucket.get("peers", []):
            if p["id"] == str(peer_id) or (p["public_key"] and p["public_key"] == str(peer_id)):
                return p
        return None


# Экземпляр по умолчанию
wgd = WGDAPI()
