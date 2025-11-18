# models.py
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import re
from typing import Optional

WAZE_URL_DEF = "https://www.waze.com/partnerhub/map-tool?lon=-100.21739443678854&lat=25.65732647938037"

@dataclass
class AppConfig:
    # Archivo / Navegador
    excel_path: str = "Captura_Waze.xlsx"
    headless: bool = False
    perfil_persistente: bool = True
    log_level: str = "INFO"
    waze_url: str = WAZE_URL_DEF

    # Horario
    modo_247: bool = True
    hora_ini_h: int = 6
    hora_ini_m: int = 0
    hora_fin_h: int = 22
    hora_fin_m: int = 0

    # Periodicidad (min)
    periodicidad_min: int = 10

    # UI / lógica
    refresh_window_sec: int = 60

    def hora_ini_tuple(self) -> tuple[int, int]:
        return (int(self.hora_ini_h) % 24, int(self.hora_ini_m) % 60)

    def hora_fin_tuple(self) -> tuple[int, int]:
        return (int(self.hora_fin_h) % 24, int(self.hora_fin_m) % 60)


@dataclass
class TramoNorm:
    nombre: str
    dist_km: float | None
    tiempo_min: int | None
    tiempo_seg: int | None
    tiempo_mmss: str
    vel_kmh: float | None
    jam: int | None
    es_usual: Optional[bool]   # <- antes: bool
    current_raw: str = ""
    historic_raw: str = ""
    dist_raw: str = ""
    raw: str = ""


# ----------------- Utilidades de horario -----------------
def _dt_con_hora(base: datetime, hh: int, mm: int) -> datetime:
    return base.replace(hour=hh, minute=mm, second=0, microsecond=0)

def esta_dentro_horario(cfg: AppConfig, dt: datetime) -> bool:
    if cfg.modo_247:
        return True
    hi = _dt_con_hora(dt, *cfg.hora_ini_tuple())
    hf = _dt_con_hora(dt, *cfg.hora_fin_tuple())
    if hi <= hf:
        return hi <= dt < hf
    else:
        return not (hf <= dt < hi)

def proximo_inicio_desde(cfg: AppConfig, dt: datetime) -> datetime:
    hi_h, hi_m = cfg.hora_ini_tuple()
    if esta_dentro_horario(cfg, dt):
        return dt
    if (hi_h, hi_m) == cfg.hora_fin_tuple():
        return _dt_con_hora(dt + timedelta(days=1), hi_h, hi_m)
    if _dt_con_hora(dt, hi_h, hi_m) > dt:
        return _dt_con_hora(dt, hi_h, hi_m)
    return _dt_con_hora(dt + timedelta(days=1), hi_h, hi_m)

def alinear_a_intervalo(desde: datetime, intervalo_seg: int) -> datetime:
    epoch = int(desde.timestamp())
    resto = epoch % intervalo_seg
    if resto == 0:
        return desde.replace(second=0, microsecond=0)
    return desde + timedelta(seconds=(intervalo_seg - resto))

def siguiente_captura_inicial(cfg: AppConfig, intervalo_seg: int) -> datetime:
    ahora = datetime.now()
    if not esta_dentro_horario(cfg, ahora):
        inicio = proximo_inicio_desde(cfg, ahora)
        return alinear_a_intervalo(inicio, intervalo_seg)
    return alinear_a_intervalo(ahora, intervalo_seg)

def fmt_restante(segundos: int) -> str:
    if segundos < 0: segundos = 0
    h, rem = divmod(segundos, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ----------------- Utilidades de nombres -----------------
_sheet_forbidden = r'[:\\/*?\[\]]'
def nombre_hoja_seguro(nombre_original: str) -> str:
    """
    Excel limita a 31 chars y prohíbe : \ / ? * [ ].
    Hacemos un hash corto para evitar colisiones con truncado.
    """
    base = (nombre_original or "").strip()
    base = base.replace(":", " -").replace("/", " /")
    base = re.sub(_sheet_forbidden, " ", base)
    base = base[:27]  # deja espacio sufijo
    suf = f"_{abs(hash(nombre_original))%1000:03d}"
    return (base + suf)[:31]
