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
    "Re&load from Disk": "Diskten Ye&niden Yükle",
    "Reload from disk?": "Diskten yeniden yüklensin mi?",
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
    # Kopyala takes p and Yapıştır takes r: G, Y, D, T, A, K and S are all spoken for in
    # this menu already, and an item whose accelerator is claimed cannot be reached from
    # the keyboard at all.
    "Cop&y": "Ko&pyala",
    "&Paste": "Yapıştı&r",
    "Dupl&icate": "&Çoğalt",
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
    "&Cut Track": "Şeridi &Kes",
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
    "Measure &Distance": "&Mesafe Ölç",
    "&Go to Part…": "Parçaya &Git…",
    "Go to Part": "Parçaya Git",
    "Filter parts…  (R37, 10k, TO-220, C7)": "Parçaları süz…  (R37, 10k, TO-220, C7)",
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
    "Cutting tracks": "Şerit kesiliyor",
    "click a hole, Esc ends": "bir deliğe tıkla, Esc bitirir",
    "Measuring": "Ölçülüyor",
    "click two holes": "iki deliğe tıkla",
    "from": "başlangıç",
    "Esc ends": "Esc bitirir",
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
    # -- board setup: which kind of board this is -----------------------------
    "Type": "Tip",
    "Strips run": "Şeritler",
    "Pad per hole — every hole is its own island": (
        "Delik başına ada — her delik kendi adası"
    ),
    "Stripboard — whole rows joined; you cut the track to separate": (
        "Şeritli plaket — satırlar baştan bağlı; ayırmak için şerit kesilir"
    ),
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
    # -- a part's own properties ----------------------------------------------
    "Proper&ties…": "Ö&zellikler…",
    "Part Properties": "Parça Özellikleri",
    "Reference": "Referans",
    "Value": "Değer",
    "Footprint": "Ayak izi",
    "Pin 1 at": "1. pin",
    "Height": "Yükseklik",
    "locked — auto-placement leaves it where it is": (
        "kilitli — otomatik yerleşim onu yerinde bırakır"
    ),
    "A part needs a reference": "Parçanın bir referansı olmalı",
    "Every part is identified by its reference, so it cannot be blank.": (
        "Her parça referansıyla tanınır, dolayısıyla boş bırakılamaz."
    ),
    "Cannot change this part": "Bu parça değiştirilemedi",
    "Properties edits one part at a time — select a single part.": (
        "Özellikler tek seferde tek parçayı düzenler — tek bir parça seç."
    ),
    "The part's reference and value. The value is what the build guide's bill "
    "of materials groups on, and nothing else in the window can set it.": (
        "Parçanın referansı ve değeri. Montaj rehberinin malzeme listesi değere göre "
        "gruplanır ve bu değeri pencerede başka hiçbir şey belirleyemez."
    ),
    "The designator the schematic uses. Renaming one that a net names takes "
    "it off that net, so rename before importing a netlist rather than after.": (
        "Şemanın kullandığı tanımlayıcı. Bir netin adıyla andığı parçayı yeniden "
        "adlandırmak onu o netten çıkarır; bu yüzden netlist içe aktarıldıktan sonra "
        "değil, öncesinde adlandır."
    ),
    "What the part actually is. This is the column the build guide's bill of "
    "materials groups on, so a blank one becomes a line you cannot order.": (
        "Parçanın gerçekte ne olduğu. Montaj rehberinin malzeme listesi bu sütuna göre "
        "gruplanır; boş bırakılırsa sipariş edilemeyecek bir satır olur."
    ),
    "Value for parts placed now…  (10k, 100nF)": (
        "Şimdi yerleştirilecek parçaların değeri…  (10k, 100nF)"
    ),
    "Given to each part as it is placed. Leave it blank and the part is placed "
    "without one; F2 sets it afterwards either way.": (
        "Yerleştirilen her parçaya verilir. Boş bırakılırsa parça değersiz yerleştirilir; "
        "her hâlükârda F2 ile sonradan da girilebilir."
    ),
    "Click a hole to place": "Yerleştirmek için bir deliğe tıkla:",
    "Esc cancels.": "Esc iptal eder.",
    "Pick a part, then click the board. Esc cancels.": (
        "Bir parça seç, sonra karta tıkla. Esc iptal eder."
    ),
    # -- panel headings --------------------------------------------------------
    "Part": "Parça",
    "Pins": "Pin",
    "Rule / Kind": "Kural / Tür",
    "Message": "Mesaj",
    "Filter findings…  (error, short, R5', C7)": "Bulguları süz…  (error, short, R5', C7)",
    "pads": "ped",
    # "To route", not "Left": the board edge in Board Features is already called Left,
    # and one English key cannot carry two meanings in a catalogue whose keys ARE the
    # English strings. Saying what the number counts is better English anyway.
    "To route": "Kalan",
    # -- the build guide, in the window ----------------------------------------
    "Build Guide": "Montaj Rehberi",
    "Show &Build Guide": "&Montaj Rehberini Göster",
    "The soldering order, in the window: shortest part first, jumpers before "
    "whatever stands on them, ICs last. Picking a step shows it on the board.": (
        "Lehimleme sırası, pencerenin içinde: önce en alçak parça, üzerinde bir şey duran "
        "jumperlar ondan önce, entegreler en sonda. Bir adımı seçmek onu kart üzerinde gösterir."
    ),
    "Step": "Adım",
    "Export the Guide…": "Rehberi Dışa Aktar…",
    "Build guide written": "Montaj rehberi yazıldı",
    "Written to {folder}, with {count} other files.": (
        "{folder} içine yazıldı, {count} dosya daha."
    ),
    "Open the Guide": "Rehberi Aç",
    "Show the Folder": "Klasörü Göster",
    "Close": "Kapat",
    # -- right-click -----------------------------------------------------------
    "&Copy This Finding": "Bu Bulguyu &Kopyala",
    "Copy &All Findings": "&Tüm Bulguları Kopyala",
    "findings copied to the clipboard": "bulgu panoya kopyalandı",
    # -- the language of the interface itself ----------------------------------
    "&Language": "&Dil",
    "English": "İngilizce",
    "Turkish": "Türkçe",
    "Language changed": "Dil değişti",
    "The interface is built in one language when the window opens, so the new "
    "one appears the next time PerfStudio starts.": (
        "Arayüz, pencere açılırken tek bir dilde kurulur; yeni dil PerfStudio'nun "
        "bir sonraki açılışında görünür."
    ),
    # -- tooltips: what each command actually does -----------------------------
    # Every one of these was English in a Turkish interface, which is the half that
    # matters: a menu item names a command, and the tooltip is where it is explained.
    "Load the file again, discarding what is in this window. The board reloads "
    "itself automatically when it changes on disk and there is nothing unsaved.": (
        "Dosyayı yeniden yükler, bu penceredekini atar. Kaydedilmemiş bir şey yoksa kart, "
        "diskte değiştiğinde kendini zaten otomatik yeniler."
    ),
    "Grid size and substrate. The material is not cosmetic: it decides the iron "
    "temperature the build guide gives and whether the pad-lifting rule applies.": (
        "Izgara ölçüsü ve taban malzemesi. Malzeme süs değildir: montaj rehberindeki "
        "havya sıcaklığını ve pad kalkması kuralının geçerli olup olmadığını belirler."
    ),
    "Mounting holes and edge-connector fingers. A mounting bore takes the copper "
    "off the pads around it, so DRC treats a pin left there as an error.": (
        "Montaj delikleri ve kenar konnektörü parmakları. Montaj deliği çevresindeki "
        "padlerin bakırını alır; bu yüzden orada kalan bir pini DRC hata sayar."
    ),
    "Write the step-by-step soldering guide: one offline HTML file, the wire cut "
    "list and BOM as CSV, and the whole thing as JSON.": (
        "Adım adım lehimleme rehberini yazar: çevrimdışı tek bir HTML dosyası, kablo "
        "kesim listesi ve malzeme listesi CSV olarak, tamamı da JSON olarak."
    ),
    "Put the selected parts and copper on the clipboard as text, so a block can "
    "be pasted into another board, another window, or a bug report.": (
        "Seçili parçaları ve bakırı metin olarak panoya koyar; böylece bir blok başka bir "
        "karta, başka bir pencereye ya da bir hata bildirimine yapıştırılabilir."
    ),
    "Place the clipboard's block under the pointer. New references, no net "
    "claim: a copy of R1 is not R1, and its copper is not on R1's net.": (
        "Panodaki bloğu imlecin altına yerleştirir. Yeni referanslar, net iddiası yok: "
        "R1'in kopyası R1 değildir ve bakırı da R1'in netinde değildir."
    ),
    "Copy and paste the selection in one step, beside itself and without "
    "touching the clipboard.": (
        "Seçimi tek adımda kopyalayıp yanına yapıştırır, panoya dokunmadan."
    ),
    "Break the strip at a hole. The cut is drilled through the pad, so that hole "
    "has nothing to solder to afterwards — click a cut again to take it back.": (
        "Şeridi bir delikte koparır. Kesik padin içinden delinir, dolayısıyla o deliğin "
        "ardından lehimlenecek bir şeyi kalmaz — kesiği geri almak için tekrar tıkla."
    ),
    "Leave any board mode: placing a part, drawing a conductor, connecting pins.": (
        "Hangi kart kipindeysen bırakır: parça yerleştirme, iletken çizme, pin bağlama."
    ),
    "Rearrange the unlocked parts to shorten the connections and make them "
    "solderable as traces rather than wires. Shows the result before applying it.": (
        "Kilitli olmayan parçaları, bağlantıları kısaltmak ve kabloyla değil lehim yoluyla "
        "yapılabilir kılmak için yeniden düzenler. Sonucu uygulamadan önce gösterir."
    ),
    "Search again from a different seed. Annealing is a random walk, so this is "
    "a real second answer rather than the same one twice.": (
        "Farklı bir tohumdan yeniden arar. Tavlama rastgele bir yürüyüştür; bu yüzden bu, "
        "aynı cevabın tekrarı değil gerçek bir ikinci cevaptır."
    ),
    "Click one pin, then another. They end up on the same net: an existing one if "
    "either pin is already on it, or a new one named for you if neither is.": (
        "Bir pine, sonra bir başkasına tıkla. İkisi aynı nete girer: pinlerden biri zaten "
        "bir netteyse o net, hiçbiri değilse senin için adlandırılan yeni bir net."
    ),
    "Name a net, then click its pins on the board. Nothing here needs KiCad.": (
        "Bir nete ad ver, sonra pinlerine kart üzerinde tıkla. Burada KiCad gerekmez."
    ),
    "Click each pin that belongs to the selected net. Right-click or Enter "
    "finishes, and the whole session goes on the history as one step.": (
        "Seçili nete ait her pine tıkla. Sağ tık veya Enter bitirir ve oturumun tamamı "
        "geçmişe tek adım olarak geçer."
    ),
    "Name, class, and the current and voltage it carries — which nothing else in "
    "the application can set, and which DRC's capacity and creepage rules need.": (
        "Ad, sınıf ve taşıdığı akım ile gerilim — bunları uygulamada başka hiçbir şey "
        "belirleyemez ve DRC'nin kapasite ile yüzeysel kaçak kuralları bunlara ihtiyaç duyar."
    ),
    "Take the pins selected in the Nets panel off their net. Expand a net to "
    "see them.": (
        "Netler panelinde seçili pinleri netlerinden çıkarır. Görmek için neti genişlet."
    ),
    "Forget what the net was for. Copper already laid for it stays on the board, "
    "and stops being anything re-route or the stale sweep will touch.": (
        "Netin ne için olduğunu unutur. Onun için döşenmiş bakır kartta kalır ve artık ne "
        "yeniden route'un ne de artık temizliğinin dokunacağı bir şey olur."
    ),
    "Rip up the existing routing and plan it again from nothing. Use this after "
    "moving parts: autoroute only adds, so it leaves the copper laid out for "
    "where things used to be. Hand-drawn copper with no net is never touched.": (
        "Mevcut route'u söküp sıfırdan yeniden planlar. Parçaları taşıdıktan sonra bunu "
        "kullan: otomatik route yalnızca ekler, dolayısıyla eski konumlar için döşenmiş "
        "bakırı yerinde bırakır. Neti olmayan elle çizilmiş bakıra asla dokunulmaz."
    ),
    "Draw conductors on the face you are NOT looking at as hatched, the way a part "
    "on the far side already is. Turn it off to see them solid.": (
        "Bakmadığın yüzdeki iletkenleri, karşı yüzdeki bir parçanın çizildiği gibi taralı "
        "çizer. Dolu görmek için kapat."
    ),
    "Click two holes. Says how many holes across they are, how far apart in mm, "
    "and how many steps of solder trace it would take to join them — three "
    "different numbers that answer three different questions.": (
        "İki deliğe tıkla. Kaç delik ötede olduklarını, mm cinsinden aralarındaki mesafeyi "
        "ve birleştirmek için kaç adım lehim yolu gerektiğini söyler — üç ayrı soruyu "
        "yanıtlayan üç ayrı sayı."
    ),
    "Find a part by reference, value or footprint and centre the view on it. "
    "On a dense board there is otherwise no way to answer “where is R37”.": (
        "Bir parçayı referansından, değerinden ya da ayak izinden bulur ve görünümü ona "
        "ortalar. Kalabalık bir kartta “R37 nerede” sorusunun başka yanıtı yoktur."
    ),
    "Open the 3D board view (Ctrl+3). Closed by default: it is the "
    "most expensive thing in the window to keep up to date.": (
        "3D kart görünümünü açar (Ctrl+3). Varsayılan olarak kapalıdır: pencerede güncel "
        "tutulması en pahalı şeydir."
    ),
    "Green for FR-4 and brown for phenolic, which is what those substrates "
    "actually look like.": (
        "FR-4 için yeşil, pertinaks için kahverengi; bu tabanlar gerçekten böyle görünür."
    ),
    "Every binding, read off this menu bar — plus the board gestures, which are "
    "on no menu and were previously only in the source.": (
        "Her kısayol, bu menü çubuğundan okunarak — artı hiçbir menüde olmayan ve daha "
        "önce yalnızca kaynak kodda bulunan kart hareketleri."
    ),
    "Wakes DRC's current-capacity rule and picks the wire gauge on the build "
    "guide's cut list. Nothing else in the application can set it.": (
        "DRC'nin akım kapasitesi kuralını uyandırır ve montaj rehberinin kesim listesindeki "
        "kablo kalınlığını seçer. Uygulamada bunu başka hiçbir şey belirleyemez."
    ),
    "Wakes DRC's creepage rule above the mains threshold. A -12 V rail is an "
    "ordinary value here, which is why it needs its own tick rather than a zero.": (
        "Şebeke eşiğinin üstünde DRC'nin yüzeysel kaçak kuralını uyandırır. -12 V'luk bir "
        "ray burada sıradan bir değerdir; bu yüzden sıfır yerine kendi kutucuğu gerekir."
    ),
    "This board prints its own addresses, so the editor's ruler would repeat them.": (
        "Bu kart kendi adreslerini basıyor, düzenleyicinin cetveli onları tekrarlar."
    ),
    "Column letters and row numbers along the edges of the view.": (
        "Görünümün kenarları boyunca sütun harfleri ve satır numaraları."
    ),
    "Only stripboard has tracks to cut. File ▸ Board Setup ▸ Type.": (
        "Yalnızca şeritli plakette kesilecek şerit vardır. Dosya ▸ Kart Ayarları ▸ Tip."
    ),
    # -- the status bar's fixed sentences --------------------------------------
    "Nothing to place: the board is empty.": "Yerleştirilecek bir şey yok: kart boş.",
    "Select a net in the Nets panel, or a part on the board, then route.": (
        "Netler panelinden bir net ya da kart üzerinden bir parça seç, sonra route et."
    ),
    "Select a net in the Nets panel, or a part on the board, then re-route.": (
        "Netler panelinden bir net ya da kart üzerinden bir parça seç, sonra yeniden route et."
    ),
    "No netlist imported, so there is nothing to route.": (
        "İçe aktarılmış netlist yok, dolayısıyla route edilecek bir şey de yok."
    ),
    "No stale conductors: every one still connects the net it claims.": (
        "Artık iletken yok: her biri hâlâ iddia ettiği neti bağlıyor."
    ),
    "Select a part or a conductor on the board first, then copy it.": (
        "Önce kart üzerinden bir parça ya da iletken seç, sonra kopyala."
    ),
    "Select a part or a conductor on the board first, then duplicate it.": (
        "Önce kart üzerinden bir parça ya da iletken seç, sonra çoğalt."
    ),
    "There is no block on the clipboard. Copy a part or some copper first.": (
        "Panoda blok yok. Önce bir parça ya da biraz bakır kopyala."
    ),
    "That block does not fit on this board.": "O blok bu karta sığmıyor.",
    "There are no parts on this board yet.": "Bu kartta henüz parça yok.",
    "Click a hole to cut the strip there; click a cut again to take it back. "
    "Esc ends.": (
        "Şeridi kesmek için bir deliğe tıkla; kesiği geri almak için tekrar tıkla. "
        "Esc bitirir."
    ),
    "This board has never been saved, so there is nothing on disk to reload.": (
        "Bu kart hiç kaydedilmedi, dolayısıyla diskte yeniden yüklenecek bir şey yok."
    ),
    "Click a pin, then the pin it joins. Neither on a net yet? One gets made. "
    "Esc cancels.": (
        "Bir pine, sonra birleşeceği pine tıkla. Hiçbiri bir nette değilse yeni bir net "
        "oluşturulur. Esc iptal eder."
    ),
    "Select the pins to disconnect in the Nets panel — expand a net to see "
    "them.": (
        "Ayrılacak pinleri Netler panelinden seç — görmek için bir neti genişlet."
    ),
    "Select one net in the Nets panel first.": "Önce Netler panelinden tek bir net seç.",
    "Opening the 3D view builds it — this takes a moment.": (
        "3D görünümü açmak onu kurar — bu biraz sürer."
    ),
    # -- dialog titles ---------------------------------------------------------
    "Re-route the nets whose parts moved?": (
        "Parçaları taşınan netler yeniden route edilsin mi?"
    ),
    "Some connections could not be made": "Bazı bağlantılar yapılamadı",
    "Some connections could not be routed": "Bazı bağlantılar route edilemedi",
    "Imported with warnings": "Uyarılarla içe aktarıldı",
    "Place the missing parts?": "Eksik parçalar yerleştirilsin mi?",
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
