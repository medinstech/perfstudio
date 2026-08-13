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
    "Board &Features…": "Kart &Öğeleri…",
    "Open &Recent": "&Son Kullanılanlar",
    "(nothing yet)": "(henüz yok)",
    "&Clear List": "Listeyi &Temizle",
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
    "&Stop the Current Tool": "Aracı &Bırak",
    # -- nets ----------------------------------------------------------------
    # "&Net" becomes the plural "Netler": a menu named with the same word in both
    # languages would be an untranslated entry, which the catalogue tests refuse.
    "&Net": "&Netler",
    "&Connect Two Pins": "&İki Pini Bağla",
    "&New Net…": "&Yeni Net…",
    "&Add Pins to Net": "Nete &Pin Ekle",
    "&Finish Adding Pins": "Pin Eklemeyi &Bitir",
    "&Edit Net…": "Neti &Düzenle…",
    "&Disconnect Selected Pins": "Seçili Pinleri &Ayır",
    "De&lete Net": "Neti Si&l",
    "New Net": "Yeni Net",
    "Edit Net": "Neti Düzenle",
    "Delete net": "Neti sil",
    "Name": "Ad",
    "Class": "Sınıf",
    "Signal": "Sinyal",
    "Ground — routed first, and wants a rail": "Toprak — önce route edilir, ray ister",
    "Power — routed after ground, same reason": "Güç — topraktan sonra route edilir, aynı sebeple",
    "Current": "Akım",
    "Voltage": "Gerilim",
    "not stated": "belirtilmedi",
    "state a voltage": "gerilim belirt",
    # -- place and route -----------------------------------------------------
    "&Auto-place Board": "&Otomatik Yerleştir",
    "&Try Another Arrangement": "&Başka Bir Yerleşim Dene",
    "&Autoroute All Nets": "Tüm Netleri &Otomatik Route Et",
    "Route Nets of &Selection": "&Seçimin Netlerini Route Et",
    "Re-route &Everything": "Her Şeyi &Yeniden Route Et",
    "Re-route Nets of Se&lection": "Seçimin Netlerini Yeniden Route &Et",
    "Remove S&tale Conductors": "&Artık İletkenleri Kaldır",
    "&Preferred Connection": "&Tercih Edilen Bağlantı",
    "&Try each and keep the best": "&Hepsini dene, en iyisini tut",
    "&Solder trace where possible": "Mümkün olan her yerde &lehim yolu",
    "&Balanced": "&Dengeli",
    "&Wire where possible": "Mümkün olan her yerde &kablo",
    "Bend component &legs where possible": "Mümkün olan her yerde &bacak bük",
    "Preferred connection": "Tercih edilen bağlantı",
    "applies to the next route": "bir sonraki route'ta geçerli olur",
    # -- view ----------------------------------------------------------------
    "Flip Board (component / solder side)": "Kartı Çevir (komponent / lehim yüzü)",
    "&Fit Board": "Karta &Sığdır",
    "Zoom &In": "&Yakınlaştır",
    "Zoom &Out": "&Uzaklaştır",
    "Show &Ratsnest": "&Bağlantı Ağını Göster",
    "Show Hole &Addresses": "&Delik Adreslerini Göster",
    "&Hatch Copper on the Far Side": "Karşı Yüzdeki Bakırı &Taralı Göster",
    "Show &3D View": "&3D Görünümü Göster",
    "Reset 3D &Camera": "3D &Kamerayı Sıfırla",
    "Board &Colour": "Kart &Rengi",
    "Follow the &material": "&Malzemeye göre",
    "Green (FR-4)": "Yeşil (FR-4)",
    "Blue": "Mavi",
    "Black": "Siyah",
    "Red": "Kırmızı",
    "Purple": "Mor",
    "White": "Beyaz",
    "Orange (phenolic)": "Turuncu (pertinaks)",
    "&Keyboard Shortcuts…": "&Klavye Kısayolları…",
    "&About PerfStudio": "PerfStudio &Hakkında",
    # -- the shortcut card, the mode banner and the empty-board guidance -------
    "Keyboard Shortcuts": "Klavye Kısayolları",
    "Action": "Eylem",
    "Shortcut": "Kısayol",
    "On the board": "Kart üzerinde",
    "Undo": "Geri al:",
    "Nothing to undo": "Geri alınacak bir şey yok",
    "Redo the command you just took back": "Az önce geri aldığın komutu yinele",
    "Nothing to redo": "Yinelenecek bir şey yok",
    "Placing": "Yerleştiriliyor:",
    "click a hole, Esc cancels": "bir deliğe tıkla, Esc iptal eder",
    "Drawing": "Çiziliyor:",
    "click both ends, Esc cancels": "iki ucu da tıkla, Esc iptal eder",
    "click each pad, Enter or right-click finishes, Esc cancels":
        "her pede tıkla, Enter veya sağ tık bitirir, Esc iptal eder",
    "Adding pins to": "Pin ekleniyor:",
    "Enter or right-click finishes, Esc cancels": "Enter veya sağ tık bitirir, Esc iptal eder",
    "no pins yet": "henüz pin yok",
    "Nothing on this board yet.": "Bu kartta henüz bir şey yok.",
    "Pick a part from the Parts panel and click a hole to place it.":
        "Parçalar panelinden bir parça seç ve yerleştirmek için bir deliğe tıkla.",
    "Then Net ▸ New Net… to say what joins what, and Route ▸ Autoroute.":
        "Sonra Netler ▸ Yeni Net… ile neyin neye bağlanacağını söyle, ardından Yol ▸ Otomatik.",
    "An existing circuit comes in through File ▸ Import KiCad Netlist.":
        "Mevcut bir devre Dosya ▸ KiCad Netlist İçe Aktar ile gelir.",
    "Filter nets…  (gnd, power, U1)": "Netleri süz…  (gnd, power, U1)",
    "Filter parts…  (resistor, dip, 5mm)": "Parçaları süz…  (direnç, dip, 5mm)",
    # -- toolbar ---------------------------------------------------------------
    # Short labels for the buttons; the menus keep the full wording. Qt draws an action's
    # iconText on a toolbar and its text in a menu, so these are the only place they differ.
    "Connect": "Bağla",
    "Trace": "Yol",
    "Spine": "Omurga",
    "Bare": "Çıplak",
    "Insulated": "İzoleli",
    "Jumper": "Üst Jumper",
    "Auto-place": "Oto-yerleşim",
    "Autoroute": "Oto-yol",
    "Rotate": "Döndür",
    "Mirror": "Aynala",
    "Delete": "Sil",
    # "Flip Side" was here until the toolbar stopped making its own action for it and
    # started sharing the View menu's, whose button label is "Flip".
    "Flip": "Çevir",
    "Ratsnest": "Bağlantı Ağı",
    "3D": "3B",
    "Fit": "Sığdır",
    # -- the connect tool ------------------------------------------------------
    "Connecting": "Bağlanıyor",
    "click the first pin, Esc cancels": "ilk pine tıkla, Esc iptal eder",
    "Connecting from": "Bağlantı başlangıcı:",
    "click the pin it joins, Esc cancels": "birleşeceği pine tıkla, Esc iptal eder",
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
    # -- board setup: the sizes you can buy -----------------------------------
    "Board": "Kart",
    "Custom size": "Özel ölçü",
    "Double-sided green, plated holes": "Çift yüz yeşil, metalize delikli",
    "Single-sided orange phenolic": "Tek yüz turuncu pertinaks",
    # -- board setup: pad shape and the printed legend ------------------------
    "Pad shape": "Pad şekli",
    "Pad length": "Pad uzunluğu",
    "Long axis": "Uzun eksen",
    "Round": "Yuvarlak",
    "Oblong — solder bridges easily along the long axis": (
        "Oval — lehim uzun eksen boyunca kolayca köprü yapar"
    ),
    "Down a column": "Sütun boyunca",
    "Along a row": "Satır boyunca",
    "Addresses printed on the board": "Adresler kartın üzerine basılı",
    "Boards carrying their own A-Z / 01-22 legend, printed on the board itself.": (
        "Kendi A-Z / 01-22 cetvelini taşıyan, cetveli kartın üzerine basılı kartlar."
    ),
    "Row digits": "Satır basamağı",
    '2 prints row 7 as "07", the way most such boards do.': (
        '2, 7. satırı "07" olarak basar; bu kartların çoğu böyle yapar.'
    ),
    # -- board features: mounting holes and edge connectors -------------------
    "Board Features": "Kart Öğeleri",
    "Feature": "Öğe",
    "Where": "Yer",
    "Size": "Ölçü",
    "Remove": "Kaldır",
    "Mounting hole": "Montaj deliği",
    "Edge connector": "Kenar konnektörü",
    "Hole diameter": "Delik çapı",
    "Inset (holes)": "İçeri kaçıklık (delik)",
    "How many holes in from each corner; 0 uses the corner hole itself.": (
        "Her köşeden kaç delik içeride; 0, köşe deliğinin kendisini kullanır."
    ),
    "Add Corner Holes": "Köşe Delikleri Ekle",
    "Add Edge Connector": "Kenar Konnektörü Ekle",
    "Edge": "Kenar",
    "Top": "Üst",
    "Bottom": "Alt",
    "Left": "Sol",
    "Right": "Sağ",
    "First hole": "İlk delik",
    "Fingers": "Parmak sayısı",
    "That inset does not fit on this board.": "Bu kaçıklık bu karta sığmıyor.",
    "&Exploded View": "&Patlatılmış Görünüm",
    "Play": "Oynat",
    "Pause": "Duraklat",
    "Play the build from here, one step at a time.": (
        "Montajı buradan itibaren adım adım oynatır."
    ),
    "Drag back to see the board part-way through the build.": (
        "Geriye sürükleyerek kartın montajın ortasındaki hâlini görün."
    ),
    "Finished board": "Bitmiş kart",
    "Bare board": "Boş kart",
    "Lift every part off the board, with a line down to the holes it goes in.": (
        "Her parçayı karttan kaldırır; girdiği deliklere inen bir çizgi ile birlikte."
    ),
    "Height limit": "Yükseklik sınırı",
    "No limit": "Sınırsız",
    "Set Height Limit": "Yükseklik Sınırını Uygula",
    "Clear height inside the case, above the board. Taller parts are reported by DRC.": (
        "Kutunun içinde, kartın üstünde kalan net yükseklik. "
        "Daha yüksek parçaları DRC bildirir."
    ),
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
