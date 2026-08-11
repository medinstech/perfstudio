"""Translating the interface (PLAN.md milestone M7).

WHY NOT Qt LINGUIST. Qt's own mechanism -- ``tr()``, ``.ts`` files, ``lrelease`` -- is
the obvious answer for a Qt application and is the wrong one here. It needs a build step
producing binary ``.qm`` files, which this project has no build step to hang off; the
catalogue lives in an XML format nothing else in the repo can read; and a missing or
stale translation is invisible until someone runs the application in that language. This
module is a dict, checked by tests, and it costs one function call at each string.

THE RULES, both enforced by tests/test_i18n.py:

  EVERY KEY IS THE ENGLISH STRING. There is no separate identifier to keep in step with
  anything, so a translation cannot silently attach to the wrong message, and English is
  never "missing" -- it is the key.

  THE CATALOGUE MAY NOT DRIFT. A test scans the UI source for every translated literal
  and fails if the catalogue names a string the interface no longer has, which is how a
  translation file usually rots. Missing translations are allowed and fall through to
  English, because a half-translated interface is useful and a crash is not.

WHAT IS NOT TRANSLATED, deliberately: hole addresses (``C7``), DRC rule ids, net names,
component references, file paths and the engine's own messages. The addresses are the
tool's vocabulary and are the same in every language; the rule ids are identifiers.
DRC and LVS message text is generated in the engine, which has no UI dependency and is
where the differential proof lives -- translating it there would mean translating
strings that golden fixtures compare byte for byte.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: Languages with a catalogue. English is the source language and needs none.
AVAILABLE = ("en", "tr")

TURKISH: Mapping[str, str] = {
    # -- menus ---------------------------------------------------------------
    "&File": "&Dosya",
    "&Edit": "Dü&zen",
    "&Draw": "Çi&z",
    "&Place": "&Yerleştir",
    "&Route": "&Yol",
    "&View": "&Görünüm",
    "&Help": "&Yardım",
    # -- file ----------------------------------------------------------------
    "&New Board…": "&Yeni Kart…",
    "&Open…": "&Aç…",
    "&Save": "&Kaydet",
    "Save &As…": "&Farklı Kaydet…",
    # Ayar&ları, not &Ayarları: "&Aç…" already claims A in this menu, and two items with
    # the same accelerator means one of them cannot be reached from the keyboard at all.
    "&Board Setup…": "Kart Ayar&ları…",
    "&Import KiCad Netlist…": "KiCad Netlist &İçe Aktar…",
    "Export &Build Guide…": "&Montaj Rehberini Dışa Aktar…",
    "Export 1:1 PDF (component + solder side)…": "1:1 PDF Dışa Aktar (komponent + lehim yüzü)…",
    "Export 3D Snapshot PNG…": "3D Görüntü PNG Dışa Aktar…",
    "&Quit": "&Çıkış",
    # -- edit ----------------------------------------------------------------
    "&Undo": "&Geri Al",
    "&Redo": "&Yinele",
    "Rotate &Clockwise": "Saat Yönünde &Döndür",
    "Rotate Counter-clock&wise": "Saat Yönünün &Tersine Döndür",
    "&Mirror": "&Aynala",
    "Toggle &Lock": "&Kilidi Aç/Kapat",
    "&Delete": "&Sil",
    # -- draw ----------------------------------------------------------------
    "&Solder Trace": "&Lehim Yolu",
    "Solder Trace with S&pine": "&Omurgalı Lehim Yolu",
    "&Bare Wire": "&Çıplak Tel",
    "&Insulated Wire": "İ&zoleli Tel",
    "Top &Jumper": "Üst Yüz &Jumper",
    "&Stop Drawing": "Çizimi &Bitir",
    # -- place and route -----------------------------------------------------
    "&Auto-place Board": "&Otomatik Yerleştir",
    "&Try Another Arrangement": "&Başka Bir Yerleşim Dene",
    "&Autoroute All Nets": "Tüm Netleri &Otomatik Route Et",
    "Route Nets of &Selection": "&Seçimin Netlerini Route Et",
    "Re-route &Everything": "Her Şeyi &Yeniden Route Et",
    "Re-route Nets of Se&lection": "Seçimin Netlerini Yeniden Route &Et",
    "Remove S&tale Conductors": "&Artık İletkenleri Kaldır",
    # -- view ----------------------------------------------------------------
    "Flip Board (component / solder side)": "Kartı Çevir (komponent / lehim yüzü)",
    "&Fit Board": "Karta &Sığdır",
    "Zoom &In": "&Yakınlaştır",
    "Zoom &Out": "&Uzaklaştır",
    "Show &Ratsnest": "&Bağlantı Ağını Göster",
    "Show Hole &Addresses": "&Delik Adreslerini Göster",
    "Show &3D View": "&3D Görünümü Göster",
    "Reset 3D &Camera": "3D &Kamerayı Sıfırla",
    "&About PerfStudio": "PerfStudio &Hakkında",
    # -- toolbar -------------------------------------------------------------
    "Rotate": "Döndür",
    "Mirror": "Aynala",
    "Flip Side": "Yüzü Çevir",
    "Fit": "Sığdır",
    # -- docks ---------------------------------------------------------------
    "Parts": "Parçalar",
    "Nets": "Netler",
    "3D View": "3D Görünüm",
    "DRC / LVS": "DRC / LVS",
    # -- dialogs -------------------------------------------------------------
    "New Board": "Yeni Kart",
    "Board Setup": "Kart Ayarları",
    "Columns": "Sütun",
    "Rows": "Satır",
    "Material": "Malzeme",
    "Unsaved changes": "Kaydedilmemiş değişiklikler",
    "Open failed": "Açılamadı",
    "Import failed": "İçe aktarılamadı",
    "Export failed": "Dışa aktarılamadı",
    "Working": "Çalışıyor",
    "Cancel": "İptal",
    "Delete parts": "Parçaları sil",
    "Delete conductors": "İletkenleri sil",
    "Apply this placement?": "Bu yerleşim uygulansın mı?",
    "Re-route?": "Yeniden route edilsin mi?",
    "Placement refused": "Yerleşim reddedildi",
    "Routing refused": "Route reddedildi",
    "Re-route refused": "Yeniden route reddedildi",
    "Board not changed": "Kart değiştirilmedi",
    "The guide has gaps": "Rehberde eksikler var",
    "About PerfStudio": "PerfStudio Hakkında",
    # -- status bar ----------------------------------------------------------
    "hole": "delik",
    "component side": "komponent yüzü",
    "solder side": "lehim yüzü",
    "mirrored": "aynalanmış",
}

CATALOGUES: Mapping[str, Mapping[str, str]] = {"tr": TURKISH}

_language = "en"


def set_language(language: str | None) -> str:
    """Choose the interface language, returning the one actually in force.

    ``None`` means "work it out": PERFSTUDIO_LANG first, then the system locale, then
    English. An unknown language falls back to English rather than failing, because a
    misspelled environment variable should not stop the application starting.
    """
    global _language
    wanted = (language or _detect() or "en").lower().split("_")[0].split("-")[0]
    _language = wanted if wanted in AVAILABLE else "en"
    return _language


def language() -> str:
    return _language


def _detect() -> str | None:
    from_env = os.environ.get("PERFSTUDIO_LANG")
    if from_env:
        return from_env
    import locale

    try:
        code, _encoding = locale.getlocale()
    except ValueError:  # pragma: no cover - malformed locale on the host
        return None
    return code


def t(text: str) -> str:
    """Translate one interface string, or return it unchanged.

    Deliberately falls through rather than marking a missing translation: a
    half-translated interface is usable, and a placeholder in the middle of a menu is
    not.
    """
    catalogue = CATALOGUES.get(_language)
    if catalogue is None:
        return text
    return catalogue.get(text, text)


__all__ = ["AVAILABLE", "CATALOGUES", "TURKISH", "language", "set_language", "t"]
