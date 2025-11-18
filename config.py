# config.py
# Configuración global, persistencia de preferencias y logging centralizado.

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import tempfile
from pathlib import Path
from typing import Dict, Tuple, Any
from datetime import datetime, timedelta

# === Persistencia de config (JSON en el home del usuario) ===
CFG_PATH = Path.home() / ".captura_waze_config.json"

# === Perfil de Chrome (para Selenium) ===
PROFILE_DIR = Path.home() / ".captura_waze_chrome_profile"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

# === URL base de Waze PartnerHub (puedes cambiarla si lo necesitas) ===
WAZE_URL = "https://www.waze.com/partnerhub/map-tool?lon=-100.21739443678854&lat=25.65732647938037"

# === Defaults (los usamos para llenar huecos al cargar el JSON) ===
DEFAULTS: Dict[str, Any] = {
    "excel_path": "",
    "modo_247": True,
    "hora_ini_h": 6,
    "hora_ini_m": 0,
    "hora_fin_h": 22,
    "hora_fin_m": 0,
    "periodicidad_min": 10,      # minutos
    "headless": False,
    "perfil_persistente": True,
    "log_level": "INFO",
}

def load_cfg() -> Dict[str, Any]:
    try:
        data = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        # Completar faltantes con defaults
        for k, v in DEFAULTS.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return dict(DEFAULTS)

def save_cfg(cfg: Dict[str, Any]) -> None:
    # Guardamos solo claves conocidas para evitar crecer el archivo con basura
    safe = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS.keys()}
    try:
        CFG_PATH.write_text(json.dumps(safe, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

# === Logging centralizado ===
def _dir_escribible(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        p = d / (".writetest_" + str(os.getpid()))
        with open(p, "w", encoding="utf-8") as f:
            f.write("ok")
        try:
            p.unlink()
        except Exception:
            pass
        return True
    except Exception:
        return False

def _ruta_log_usuario(filename: str = "captura_waze.log") -> Path:
    candidatos = []
    if os.name == "nt":
        la = os.environ.get("LOCALAPPDATA")
        if la:
            candidatos.append(Path(la) / "CapturaWaze")
    candidatos.append(Path.home() / ".CapturaWaze")
    candidatos.append(Path(tempfile.gettempdir()) / "CapturaWaze")
    for d in candidatos:
        if _dir_escribible(d):
            return d / filename
    return Path(tempfile.gettempdir()) / filename

LOG_PATH = _ruta_log_usuario("captura_waze.log")

logger = logging.getLogger("captura_waze")
logger.setLevel(logging.INFO)

try:
    _handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
except Exception:
    _handler = logging.StreamHandler()

_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))

if not any(isinstance(h, (logging.handlers.RotatingFileHandler, logging.StreamHandler)) for h in logger.handlers):
    logger.addHandler(_handler)

def set_log_level(level_str: str) -> None:
    level = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }.get((level_str or "").upper(), logging.INFO)
    logger.setLevel(level)
    for h in logger.handlers:
        h.setLevel(level)
    logger.info(f"Nivel de log establecido a: {logging.getLevelName(level)}")

# === Variables “runtime” exportadas (para compatibilidad con la GUI) ===
#     Nota: la GUI normalmente actualizará estos valores después de la pantalla de inicio.
_cfg_boot = load_cfg()

modo_247: bool = bool(_cfg_boot.get("modo_247", DEFAULTS["modo_247"]))
hora_ini: Tuple[int, int] = (int(_cfg_boot.get("hora_ini_h", DEFAULTS["hora_ini_h"])),
                             int(_cfg_boot.get("hora_ini_m", DEFAULTS["hora_ini_m"])))
hora_fin: Tuple[int, int] = (int(_cfg_boot.get("hora_fin_h", DEFAULTS["hora_fin_h"])),
                             int(_cfg_boot.get("hora_fin_m", DEFAULTS["hora_fin_m"])))

# intervalo en segundos (la GUI lo recalcula al aceptar la config)
intervalo_captura: int = max(10, int(_cfg_boot.get("periodicidad_min", DEFAULTS["periodicidad_min"]))) * 60

headless_default: bool = bool(_cfg_boot.get("headless", DEFAULTS["headless"]))
perfil_persistente_default: bool = bool(_cfg_boot.get("perfil_persistente", DEFAULTS["perfil_persistente"]))
log_level_default: str = str(_cfg_boot.get("log_level", DEFAULTS["log_level"]))
excel_path_default: str = str(_cfg_boot.get("excel_path", DEFAULTS["excel_path"]))

# Helpers para que la GUI aplique su resultado y sincronice estas globals
def apply_ui_result(result: Dict[str, Any]) -> None:
    """
    Sincroniza las variables globales con lo elegido en la ventana de configuración.
    `result` debe contener: modo_247, hora_ini, hora_fin, periodicidad_min, headless, perfil_persistente, log_level, excel_path
    """
    global modo_247, hora_ini, hora_fin, intervalo_captura
    modo_247 = bool(result.get("modo_247", modo_247))
    hora_ini = tuple(result.get("hora_ini", hora_ini))  # (h, m)
    hora_fin = tuple(result.get("hora_fin", hora_fin))  # (h, m)
    intervalo_captura = max(10, int(result.get("periodicidad_min", intervalo_captura // 60))) * 60
    # Ajustar log level si lo pidieron
    if "log_level" in result:
        set_log_level(str(result["log_level"]))

# === Periodicidad en runtime (exportado para la GUI) ===
def set_runtime_period_minutes(mins: int) -> None:
    """
    Ajusta el intervalo de captura en minutos (mínimo 10) y lo guarda en la
    variable global `intervalo_captura` (en segundos).
    """
    global intervalo_captura
    intervalo_captura = max(10, int(mins)) * 60

def get_runtime_period_seconds() -> int:
    """
    Devuelve el intervalo de captura actual en segundos.
    """
    return int(intervalo_captura)

# Mantener compatibilidad con la GUI (usa este nombre como "sugerido")
intervalo_captura_sugerido: int = intervalo_captura

# === Utilidades de horario (la GUI las importa desde aquí) ===
def _dt_con_hora(base_dt: datetime, hh: int, mm: int) -> datetime:
    return base_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)

def esta_dentro_horario(dt: datetime) -> bool:
    global modo_247, hora_ini, hora_fin
    if modo_247:
        return True
    hi = _dt_con_hora(dt, hora_ini[0], hora_ini[1])
    hf = _dt_con_hora(dt, hora_fin[0], hora_fin[1])
    if hi <= hf:
        return hi <= dt < hf
    else:
        return not (hf <= dt < hi)

def proximo_inicio_desde(dt: datetime) -> datetime:
    global hora_ini, hora_fin
    if esta_dentro_horario(dt):
        return dt
    if hora_ini == hora_fin:
        return _dt_con_hora(dt + timedelta(days=1), hora_ini[0], hora_ini[1])
    if _dt_con_hora(dt, hora_ini[0], hora_ini[1]) > dt:
        return _dt_con_hora(dt, hora_ini[0], hora_ini[1])
    return _dt_con_hora(dt + timedelta(days=1), hora_ini[0], hora_ini[1])

def alinear_a_intervalo(desde: datetime, intervalo_seg: int) -> datetime:
    epoch = int(desde.timestamp())
    resto = epoch % intervalo_seg
    if resto == 0:
        return desde.replace(second=0, microsecond=0)
    salto = intervalo_seg - resto
    return desde + timedelta(seconds=salto)
