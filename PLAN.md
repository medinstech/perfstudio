# PerfStudio — Proje Planı

> Delikli plaket (perfboard) üzerine PCB gibi tasarım yapmayı, optimize bağlantılar
> çizmeyi ve bundan **çok detaylı bir lehim rehberi** üretmeyi sağlayan açık kaynak
> masaüstü uygulaması.
>
> **Durum:** uçtan uca çalışıyor ve yayında — **v0.10.0**, PyPI'da ve üç masaüstü
> platformu için kurulum paketi olarak. Açık kalan iki şey var ve ikisi de kod değil:
> M5'in dogfood testi (§11) ve kod imzalama (§12).
> **Sahip:** medinstech · **Lisans:** Apache-2.0 · **İsim:** PerfStudio, §12'de karara bağlandı
>
> Bu belge **planın kendisidir** ve yazıldığı hâlde duruyor, ki neyin öngörüldüğü ile
> neyin çıktığı yan yana okunabilsin — M0'ın Tauri/WebGL şeridi örneğin alınmadı,
> uygulama PySide6 + VTK oldu. Gerçekleşen durum §11'in altındaki notlarda, §13'te ve
> §14'te tutuluyor. Nasıl inşa edildiğinin anlatısı `CLAUDE.md`'de.

---

## 1. Tek Cümle

Bir şema netlist'ini al, delikli plakete yerleştir, bağlantıları optimize et,
doğruluğunu makine ile kanıtla, ve kullanıcının eline **adım adım lehimlenebilir,
ölçümle doğrulanabilir bir montaj rehberi** ver.

---

## 2. Kilitlenen Kararlar

| # | Karar | Seçim | Gerekçe |
|---|---|---|---|
| D1 | v1 kart tipi | **Ada bakırlı delikli plaket** (pad-per-hole) | TR'de en yaygın, en az desteklenen. Veri modeli üçünü de destekler, cila burada |
| D2 | Lisans | **Apache-2.0** | Şirket dostu, patent koruması. GPL'li DIYLC/VeroRoute kodundan tamamen bağımsız kalınacak |
| D3 | Devre girişi | **Netlist import + görsel düzenleme** | Şema editörü yazma yükü yok; LVS gücü kazanılıyor |
| D4 | Rehber çıktısı | **4'ü birden**: interaktif offline HTML · 1:1 PDF · doğrulama kontrol listesi · CSV kesim listesi + BOM | Rehber projenin farklılaştırıcısı; yarım bırakılmaz |
| D5 | Masaüstü çatısı | **Tauri v2**, Electron kaçış yolu açık | ~10MB kurulum, düşük RAM, Rust router yolu doğal. Platform adaptörü ince tutulacak |
| D6 | 3D modeller | **Parametrik üretim** | Sıfır asset, footprint ile garantili tutarlılık, temiz lisans |
| D7 | 3D kapsamı | **Tam** — montaj animasyonu + patlatılmış görünüm dahil | 3D'yi dekorasyondan öğretim aracına çeviren şey bu |
| D8 | Lehim yolu | **Birinci sınıf yol çekme primitifi** (cezalı özel durum değil) | TR delikli plaket pratiğinde asıl yöntem; güç/toprak rayları böyle çekiliyor |

**D3 nerede durdu.** Karar aynen geçerli ve genişledi: **devre önce çizilir, kart sonra
yerleştirilir** — diğer her EDA aracının çalıştığı sıra. `doc.parts` karta konmamış
parçaları tutar (`part.add` / `part.update` / `part.delete` / `part.place`,
`component.unplace`), `net.connect` bir parçanın kart üzerinde olmasını hiç istemiyordu,
ve şema paneli (`Ctrl+5`) bu ikisini bir araya getiriyor. D3'ün yazmamaya karar verdiği
şey hâlâ yazılmadı ve yazılmayacak: **geometrik şema editörü** — sembolü sürüklediğiniz,
telin köşesini kendiniz kırdığınız, sayfa koordinatını dosyaya yazan tür. Sayfa her
seferinde `schematic.py` tarafından belgeden türetiliyor; saklanan hiçbir çizim koordinatı
yok, dolayısıyla netlist ile senkron tutulacak ikinci bir gerçek de yok. Kazanılan LVS
gücü aynen duruyor, üstelik artık KiCad'siz de.

**Ek kararlar (tartışmaya açık ama varsayılan):**
- Uygulama içi AI paneli **v1 kapsamında değil**. Çekirdek motor asla AI'a bağımlı olmayacak — API anahtarı olmadan araç tam işlevli kalır.
- MCP tool sayısı hedefi **~25**, kasıtlı olarak dar (context tüketimini kontrol altında tutmak için).

---

## 3. Neden Var Olmalı — Boşluk Analizi

| Araç | Netlist import | Autoroute | DRC | Lehim rehberi | 3D | Ajan API | Açık |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| DIYLC | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ |
| VeroRoute | ✓ | ✓ | kısmi | ✗ | ✗ | ✗ | ✓ |
| striprouter | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✓ |
| PerfBoard.app | ✗ | ✗ | ✓ | ✓ | ✗ | ✗ | ✗ |
| VeeCAD | ✓ | kısmi | kısmi | ✗ | ✗ | ✗ | ✗ |
| **PerfStudio** | ✓ | ✓ | ✓ | **✓✓** | ✓ | ✓ | ✓ |

**Üç farklılaştırıcı:**

1. **Doğrulama kontrol noktalı lehim rehberi.** Netlist'ten deterministik türetilen
   ölçüm adımları: *"Blok 2 bitti → U1 pin 4 ile C3(−) arası süreklilik olmalı"*,
   *"Güç vermeden önce: GND–V+ arası > 10 kΩ olmalı"*. Hiçbir rakipte yok.
2. **Perfboard LVS.** Şema netlist'i ile fiziksel kartın bağlantı grafiği izomorfizmi.
   "Acaba doğru mu?" sorusunun makine cevabı.
3. **Ajan-yerel mimari.** MCP sunucusu + headless CLI + git-diff'lenebilir proje dosyası.

---

## 4. Alan Modeli (Çekirdek)

### 4.1 Delik adresleme
Sütun harfi + satır numarası (**A1, B7, AC12**) — insanların delikli plaket hakkında
konuşma biçimi. Rehberin dili bu olacak, dolayısıyla birinci sınıf kavram.

### 4.2 Kart
```ts
interface Board {
  type: 'pad-per-hole' | 'stripboard' | 'plain'   // v1: pad-per-hole
  cols: number; rows: number
  pitch: 2.54                      // mm
  thickness: 1.6                   // mm
  material: 'FR4' | 'FR2' | 'FR1'
  padDiameter: number; drillDiameter: number
  stripAxis?: 'horizontal' | 'vertical'           // v2, stripboard
}
```

### 4.3 Komponent
```ts
interface ComponentInstance {
  id: string; ref: string; value: string          // R1 / 10k
  footprintId: string; bodyId: string             // 2D footprint + 3D parametrik gövde
  anchor: HoleCoord                               // "A1"
  rotation: 0 | 90 | 180 | 270
  mirrored: boolean; locked: boolean
}

interface Footprint {                             // THT, grid'e hizalı
  pins: { number: string; name?: string; dx: number; dy: number }[]
  bodyOutline: Polygon                            // mm — çakışma kontrolü
  bodyHeight: number                              // 3D + gabari kontrolü
  bodyParams: BodyParams                          // parametrik 3D üretimi
  leadDiameter: number; polarized: boolean; pin1Marker: HoleCoord
}
```

### 4.4 İletken — mimarinin kalbi
Delikli plakette bağlantı tek tip değil. Her iletken bir **tür**, bir **katman** ve
bir **maliyet** taşır:

```ts
type ConductorKind =
  | 'lead-bend'           // komponent bacağı uzatılmış  → maliyet ~0, max 3-4 delik
  | 'solder-trace'        // LEHİM YOLU, saf lehim       → çok düşük/adım, uzunluk sınırlı
  | 'solder-trace-wired'  // LEHİM YOLU, omurgalı        → düşük + sabit hazırlık, sınırsız
  | 'bare-wire'           // lehim yüzü çıplak tel       → uzunluk×k, KESİŞEMEZ
  | 'insulated-wire'      // lehim yüzü izoleli tel      → uzunluk×k + sabit ceza, kesişebilir
  | 'top-jumper'          // üst yüz jumper              → yüksek ceza, gövde alanı işgal eder
  | 'strip'               // (v2) hazır bakır şerit      → bedava, kesim gerektirir

interface Conductor {
  id: string; kind: ConductorKind
  path: HoleCoord[]                 // düğüm dizisi
  netId?: string
  gauge?: number; color?: string    // AWG + renk konvansiyonu
  layerZ: number                    // 3D istif seviyesi (fiziksel çakışma önleme)
}
```

> `solder-bridge` ayrı bir tür değil — **iki padlik `solder-trace`**. Tek bir kavram,
> tek bir kural seti.

### 4.5 Bağlantı motoru
`(delik, yüz)` düğümleri üzerinde **union-find**. Her iletken komşu düğümleri
birleştirir; komponent pini kendi deliğinin üst ve alt yüzünü bağlar.
Çıktı: **fiziksel net listesi**.

> Bu motor yanlışsa her şey yanlış. Ayrı paket, ayrı test suiti, altın dosya testleri.

### 4.6 Lehim yolu — fiziksel model (D8)

Bitişik padlerin lehimle birleştirilerek oluşturulan iletken yol. TR pratiğinde
"lehim yolu çekmek". İki inşa biçimi:

| Biçim | Nasıl | Kullanım |
|---|---|---|
| **`solder-trace`** (saf) | Padler arasına doğrudan lehim akıtılır | Kısa yerel bağlantılar |
| **`solder-trace-wired`** (omurgalı) | Kalaylı bakır tel veya bacak kırpıntısı padler boyunca yatırılıp her padde lehimlenir | Güç/toprak rayları, uzun yollar |

```ts
interface SolderTrace extends Conductor {
  kind: 'solder-trace' | 'solder-trace-wired'
  path: HoleCoord[]                     // INVARIANT: 4-komşu bitişik zincir
  spine?: { material: 'tinned-copper' | 'lead-offcut'; gauge: number }
  buildup: 'light' | 'normal' | 'heavy' // kesit tahmini
}
```

**Geometrik kısıt — router'ı doğrudan belirler.**
2.54 mm pitch'te tipik pad çapı ~1.9 mm →

- **Ortogonal komşu:** merkez arası 2.54 mm, **pad kenarları arası ≈ 0.6 mm** → kolayca
  köprülenir. Lehim yolunun tek meşru yönü. Yol = pad grafiğinde **4-komşu Manhattan yolu**.
- **Çapraz komşu:** merkez arası 3.59 mm, kenar arası ≈ 1.7 mm → belirgin şekilde zor,
  fazla lehim ister. **Varsayılan kapalı**, açılırsa ağır cezalı + uyarılı.

**Elektriksel model.**
Lehim özdirenci ≈ 15 µΩ·cm (Sn63Pb37) / ≈ 13 µΩ·cm (SAC305) — bakırın (1.68 µΩ·cm)
**yaklaşık 8-9 katı**. Etkin kesit `buildup` profilinden tahmin edilir.

| Örnek: 10 pad (25.4 mm) | Direnç | 3 A'de düşüm / kayıp |
|---|---|---|
| Saf lehim, ~0.3 mm² kesit | ≈ 13 mΩ | 38 mV / 114 mW |
| 0.6 mm kalaylı bakır omurgalı | ≈ 1.35 mΩ | 4.0 mV / 12 mW |

Omurgalı satır **paralel direnç** olarak hesaplanır: bakır omurga ve çevresindeki lehim
aynı boy üzerinde birbirine yapışıktır, ikisi de akım taşır. Yalnız bakırı saymak
1.51 mΩ verirdi; lehim dalı bunu 1.35 mΩ'a çeker. `drc.ts` bu modeli kullanıyor.

→ **Omurga, direnci yaklaşık bir mertebe düşürüyor.** Araç bunu hesaplayıp söylemeli:
*"Bu net 3 A taşıyor, saf lehim yolu sınırda — omurga ekle."*

**Kesişim.** Lehim yolu lehim yüzünde yükseltilmiş fiziksel bir yapı: `bare-wire` ile
aynı katmanda, **kesişemez**. Üzerinden yalnızca `insulated-wire` atlayabilir.

**Asıl tehlike: 0.6 mm'lik komşuluk.** Lehim yolu, farklı nete ait bir padin ortogonal
komşusundan geçtiğinde kaza eseri köprülenme riski yüksektir. Delikli plaket
inşasının en yaygın arıza sebebi budur ve §5.2 R5' kuralının konusudur.

**Malzeme etkileşimi.** Ucuz pertinaks (FR-2 fenolik kâğıt) padleri, uzun süreli ısı
altında FR-4'e göre çok daha kolay **kalkar**. Uzun saf lehim yolu + FR-2 = pad kalkma
riski → §5.2 R5''.

---

## 5. Doğrulama Katmanı

### 5.1 LVS (Layout vs. Schematic)
Fiziksel net listesi ↔ şema net listesi izomorfizmi. Üç hata sınıfı:

- **OPEN** — şemada aynı net, kartta ayrı → eksik bağlantı
- **SHORT** — şemada ayrı net, kartta birleşik → kısa devre
- **FLOATING** — hiçbir nete bağlanmayan iletken

### 5.2 DRC kuralları (v1)
| # | Kural | Seviye |
|---|---|---|
| 1 | Gövde çakışması (courtyard overlap) | hata |
| 2 | Kart sınırı dışı yerleşim | hata |
| 3 | Aynı deliğe iki pin | hata |
| 4 | Çıplak tel kesişimi | hata |
| 5 | Komşu farklı-net adalar arasında dar geçit (köprü riski) | uyarı |
| **5'** | **Lehim yolu komşuluk riski**: yol, farklı nete ait bir padin ortogonal komşusundan geçiyor (≈0.6 mm) | **uyarı, yüksek öncelik** |
| **5''** | **Pad kalkma riski**: FR-2/pertinaks + saf lehim yolu uzunluğu > eşik | uyarı |
| **5'''** | Saf lehim yolu uzunluğu > 5-6 pad (yapılabilirlik/güvenilirlik) → omurga öner | uyarı |
| **5''''** | Lehim yolu çapraz adım içeriyor (varsayılan kapalı) | uyarı |
| 6 | Akım kapasitesi: net akımı vs. tel kesiti / **lehim yolu etkin kesiti** | uyarı |
| **6'** | Lehim yolu direnç/gerilim düşümü hesabı net akımına göre eşiği aşıyor | uyarı |
| 7 | Creepage: 2.54 mm delik aralığı ≈ 300 V sınırı — şebeke devrelerinde | **uyarı, kalın** |
| 8 | Yükseklik / gabari çakışması (3D'den) | uyarı |
| 9 | Isı yakınlığı: TO-220 / güç direnci yanında elektrolitik | uyarı |
| 10 | Aşırı uzun bacak bükümü (`lead-bend` > N delik) | uyarı |
| 11 | Bağlanmamış pin veya net (netlist karşılaştırması) | hata |

---

## 6. Algoritmalar

**Router** — katmanlı grid üzerinde A\*/Lee maze + **rip-up & reroute**.
Net sıralaması kritikliğe göre (güç ve toprak önce, ray olarak).

### 6.1 Maliyet tablosu

| Primitif | Adım maliyeti | Sabit maliyet | Kısıt |
|---|---|---|---|
| `lead-bend` | ~0 | 0 | ≤3-4 delik, yalnız komponent pininden |
| **`solder-trace`** | **çok düşük** | ~0 | 4-komşu · ≤5-6 pad · kesişemez · komşuluk riski |
| **`solder-trace-wired`** | **düşük** | orta (omurga hazırlama) | 4-komşu · sınırsız · kesişemez |
| `bare-wire` | uzunluk×k | orta | serbest yön · kesişemez |
| `insulated-wire` | uzunluk×k | yüksek (kes/soy/lehimle) | kesişebilir |
| `top-jumper` | uzunluk×k | çok yüksek | kesişebilir · gövde alanını işgal eder |

Ek cezalar: kesişim (bare/solder yolu için ∞, insulated için orta) · **her yeni ayrı
iletken için sabit ceza** (az sayıda uzun yol, çok sayıda kısa yoldan montajı kolaydır)
· DRC risk cezaları (R5' komşuluk riski maliyet fonksiyonuna doğrudan girer, sonradan
uyarı olarak değil).

> **R5''yi maliyete gömmek kritik.** Router, lehim yolunu farklı-net padlerin yanından
> geçirmemeyi *tercih ederse*, üretilen layout sadece geçerli değil aynı zamanda
> **lehimlenmesi kolay** olur. Rakiplerin hiçbiri yapılabilirliği maliyet fonksiyonuna
> koymuyor.

### 6.2 Ray (bus) stratejisi — yüksek fan-out netler

GND ve V+ gibi çok bacaklı netler nokta-nokta route edilmez. Delikli plakette standart
pratik: bir satır/sütun boyunca **omurgalı lehim yolu rayı** çekip pinleri kısa saplarla
raya bağlamak. Router bunu ayrı bir strateji olarak tanıyacak:

1. Net fan-out'u eşiği aşarsa ray moduna geç
2. Pin bulutuna en iyi uyan satır/sütunu seç (medyan eksen)
3. Ray = `solder-trace-wired`, saplar = `solder-trace` veya `lead-bend`
4. Rayı kart kenarına yakın tut (komşuluk riski azalır, ölçüm probu erişimi artar)

### 6.3 Yerleştirme optimizasyonu

Simulated annealing. Hamleler: ötele / döndür / iki komponenti takasla.
Maliyet: HPWL (yarım-çevre tel uzunluğu) + DRC cezaları + mekanik kısıtlar
(konnektör ve potansiyometreler kenarda, soğutuculu parçalara boşluk)
+ **lehim yolu hizalanabilirliği** (pinleri aynı satır/sütuna düşüren yerleşimler
ödüllendirilir — kısa lehim yolu, uzun telden iyidir).

**Determinizm zorunlu:** tohumlu RNG. Aynı girdi → aynı layout. Test edilebilirlik
ve kullanıcı güveni için pazarlık konusu değil.

### 6.4 Beklenti yönetimi

"Butona bas, mükemmel layout" vaadi verilmeyecek. Hedef
**interaktif asistan**: kullanıcı parçayı sürükler, etkilenen netler < 100 ms'de
yeniden route edilir, DRC canlı çalışır. Forumlardaki asıl şikâyet
("4 bağlantıyı bağlayamadan bıraktı") tam olarak bu tuzağa düşmekten kaynaklanıyor.

---

## 7. Lehim Rehberi Spesifikasyonu

### 7.1 Adım sıralaması — iki anahtarlı
Fiziksel gerçek: parçalar gruplar hâlinde takılır → kart çevrilir → lehimlenir →
bacaklar kesilir → o bölgenin bağlantıları yapılır. Dolayısıyla sıralama
**(a) fonksiyonel blok** ve **(b) gövde yüksekliği** anahtarlarıyla yapılır.

```
Faz 0  Hazırlık        malzeme listesi, alet, havya sıcaklığı, kart kesimi, A1 referans işareti
Faz 1  En alçak        üst yüz jumper telleri, yatık direnç ve diyotlar
Faz 2  Soketler        IC soketleri  (IC'lerin kendisi EN SONA — ısı + ESD)
Faz 3  Küçük gövde     seramik/film kondansatör, TO-92 transistör
Faz 4  Orta gövde      elektrolitik, TO-220, kristal
Faz 5  Yüksek/mekanik  konnektör, potansiyometre, klemens, soğutucu
Faz 6  Lehim yüzü      çıplak tel ve köprüler (blok blok, her grup takıldıkça)
Faz 7  Uzun teller     izoleli bağlantılar
Faz 8  Kapanış         IC'leri sokete tak, son kontrol, kontrollü güç verme prosedürü
```

### 7.2 Her adım kartında bulunacaklar
- Ref, değer, footprint, **tam delik koordinatları** (`R3: C7 → C11, 4 delik açıklık`)
- Polarite / oryantasyon uyarısı + pin-1 işareti
- **Bacak bükme şablonu** (`10.16 mm / 4 delik`)
- 3D'den otomatik render edilmiş, o parçanın vurgulandığı kare
- Üst görünüm **ve aynalanmış lehim yüzü görünümü** (kullanıcıların en çok hata yaptığı yer)
- Havya sıcaklığı / lehim teli / flux notu (ada büyüklüğüne göre)

### 7.3 Tel kesim listesi
```
uzunluk = Manhattan yol uzunluğu (mm)
        + 2 × (kart kalınlığı + büküm payı)
        + 2 × soyma boyu
```
Her tel için: kesit (akımdan hesaplanır), izolasyon tipi, **renk konvansiyonu**
(kırmızı = V+, siyah = GND, ...), delik-delik yol.
**Ayrı bölüm: omurga telleri** (`solder-trace-wired`) — kalaylı bakır tel uzunlukları
ve hangi bacak kırpıntısının nereye yeteceği.

### 7.4 Lehim yolu talimatları (D8)

Her lehim yolu için üretilen adım kartı:

- **Yol tanımı:** `GND rayı: B12 → K12, 10 pad, omurgalı`
- **İnşa biçimi:** saf mı, omurgalı mı; omurgalıysa tel kesiti ve boyu
- **Yön:** tek yönde çalış, soğumuş bölümün üzerine geri dönme
- **Isı bütçesi:** malzemeye göre havya sıcaklığı ve pad başına temas süresi üst sınırı
  (FR-2/pertinaks için belirgin şekilde düşük — pad kalkma riski)
- **Flux zorunlu** notu; padleri önce tek tek hafif kalayla, sonra birleştir
- **Komşuluk uyarısı (R5'):** *"C7 ve F7 padleri farklı nete ait ve yolun 0.6 mm
  yanında — bu iki noktada dikkat, sonra mutlaka izolasyon ölç"*
- **Elektriksel özet:** hesaplanan direnç, net akımında gerilim düşümü ve kayıp

**Teknik notu (otomatik metin):** 3+ pad için lehim yığmak yerine bacak kırpıntısını
veya kalaylı teli omurga olarak kullan — direnç yaklaşık bir mertebe düşer,
mekanik dayanım ve tekrar edilebilirlik belirgin artar.

### 7.5 Doğrulama kontrol noktaları — **farklılaştırıcı**
Netlist'ten deterministik üretilir:
- Her blok sonunda **süreklilik listesi** (bağlı OLMASI gerekenler)
- Her blok sonunda **izolasyon listesi** (bağlı OLMAMASI gerekenler, özellikle güç rayları)
- **DRC risk listesi doğrudan test listesine dönüşür.** R5' ile işaretlenen her
  komşuluk riski, o lehim yolu bitince yapılacak somut bir izolasyon ölçümü olur:
  *"C7 ↔ C8 arası direnç ölç, açık devre olmalı."* Böylece aracın öngördüğü risk ile
  kullanıcının yaptığı ölçüm aynı listeden gelir — delikli plaketin en yaygın arızası
  daha kart bitmeden yakalanır.
- Uzun lehim yolları için **uçtan uca direnç kontrolü** (hesaplanan değer ± tolerans),
  soğuk lehim ve çatlak yakalar
- Güç vermeden önce: V+ ↔ GND direnç makullüğü, elektrolitik polarite taraması, IC yönü
- Güç verme prosedürü: akım sınırlı besleme, beklenen boştaki akım (kullanıcı girerse)

### 7.6 Çıktı formatları
| Format | Not |
|---|---|
| **İnteraktif HTML** | Tek dosya, offline, adımlar tiklenir, ilerleme `localStorage`'da; karta tıklayınca ilgili adım vurgulanır |
| **PDF** | 1:1 baskı doğruluğu — üst görünüm + aynalanmış lehim yüzü, karta tutturulabilir şablon |
| **CSV** | Tel kesim listesi + BOM |
| **JSON** | Makine okunur rehber (ajanlar ve entegrasyonlar için) |

---

## 8. Mimari

### 8.1 Command Bus — tek kural
> **UI hiçbir zaman veri modelini doğrudan değiştirmez.** Her eylem bir komut
> nesnesidir ve tek bir bus'tan geçer.

```
   GUI (2D/3D) ──┐
   CLI         ──┤
   MCP Server  ──┼──►  Command Bus  ──►  Document  ──► 2D / 3D / DRC / LVS / Guide
   Makro/Script──┘      + Undo/Redo        (immutable)
```

Bu tek karardan bedava gelenler: undo/redo · makro kaydı · **deterministik replay
testleri** · oturum kaydı · ajanın ve kullanıcının aynı belgeyi eşzamanlı sürmesi.

### 8.2 Monorepo (pnpm workspaces)
```
packages/core        saf TS: doküman, command bus, connectivity (union-find),
                     router, placer, DRC, LVS, guide generator   ← DOM yok, Tauri yok
packages/parsers     KiCad netlist / footprint, SPICE netlist
packages/render2d    Canvas2D renderer            (headless çalışabilir)
packages/render3d    three.js sahne kurucu + parametrik gövde üreticileri
                                                  (headless PNG render)
packages/guide       HTML / PDF / CSV üretici (2D+3D render'ları gömer)
packages/mcp         MCP sunucusu (stdio + streamable HTTP)
apps/desktop         Tauri v2 + React             ← platform adaptörü ince
apps/cli             headless
```

**Headless render pazarlık konusu değil** — MCP `render_3d_view`, rehber üretimi ve
CI görsel testleri GUI olmadan çalışmalı.

### 8.3 2D / 3D
- **2D — authoring görünümü.** Canvas2D, viewport culling, grid'e snap.
  Perf yetmezse PixiJS/WebGL'e geçilir (renderer arayüzü izole tutulacak).
- **3D — doğrulama ve iletişim görünümü.** three.js + react-three-fiber.
  Delik ızgarası **instanced mesh** (100×60 kart = 6000 delik; instancing olmadan perf çöker).
  Basit materyal, ağır post-processing yok. Hedef "doğru ve anlaşılır", "fotogerçekçi" değil.
- **Lehim yolu görselleştirmesi (2D + 3D).** Yuvarlak telden ayrı bir görünüm gerekir:
  padlerde şişkinleşen, aralarda incelen kabarık bir zincir. `buildup` profili yüksekliği
  belirler; parametrik bir süpürme geometrisi. 3D'de metalik/parlak materyal — lehim
  yolları ile telleri bir bakışta ayırt etmek lehim yüzü görünümünün okunabilirliği
  için şart. 2D'de R5' risk noktaları kırmızı halka ile işaretlenir.
- Seçim/hover durumu iki yönlü paylaşılır. 2D'de yüz toggle'ı, 3D'de gerçek çevirme.
- **Parametrik gövdeler (~25 tip):** aksiyel direnç, DO-41 diyot, radyal elektrolitik,
  disk seramik, film kondansatör, DIP-N, TO-92, TO-220, LED 3/5/10 mm, header, klemens,
  pot, buton, kristal, röle, trafo, ...

### 8.4 3D'nin işlevsel gerekçeleri
1. Yükseklik/çarpışma kontrolü (DRC #8) — 2D'de görünmez
2. Lehim yüzü tel karmaşasının okunması
3. **Montaj animasyonu** — rehber adımlarının 3D oynatımı (D7)
4. **Rehbere otomatik adım görseli** — elle görsel hazırlama derdi biter
5. (v2) Muhafaza tasarımı: yükseklik profili + kontur → STL/GLB

---

## 9. Ajan Entegrasyonu

### 9.1 Taşıma — ikisi birden
- **stdio (birincil).** Claude Code *ve* Antigravity ikisinde de sorunsuz. GUI gerekmez,
  headless döner, CI'da koşar.
- **Streamable HTTP (localhost).** Açık duran GUI'ye bağlanmak için; ajan düzenler,
  kullanıcı canlı görür. **Yeni SSE-only sunucu yazılmayacak** (deprecated).
- **Tuzak:** tüm log **stderr**'e. Kaçak bir `console.log` stdout'u kirletip protokolü
  sessizce bozar — bilinen ve yaygın hata.

### 9.2 Tool yüzeyi (~25)
| Kategori | Tool'lar |
|---|---|
| Okuma | `get_board_info` · `list_components` · `get_component` · `get_nets` · `get_net_connections` |
| **Görme** | `render_2d_view(side, region, dpi)` · `render_3d_view(camera, exploded)` → PNG |
| Yazma | `place_component` · `move_component` · `rotate_component` · `route_net` · `add_wire` · `add_solder_bridge` · `autoroute` · `optimize_placement` |
| Doğrulama | `run_drc` · `run_lvs` · `check_heights` |
| Çıktı | `generate_guide` · `export_pdf` · `export_csv` |
| Durum | `snapshot` · `restore` · `undo` · `redo` |

**En kritik ikisi:**
- `render_*` — görsel geri bildirim olmadan ajan kör çalışır. Kartı *görebilmeli*.
- `snapshot`/`restore` — ajan deneyip geri alabilmeli.

### 9.3 Proje dosyası ajan-dostu
Stabil anahtar sıralamalı, pretty-print JSON (`.perf` uzantısı, içi JSON).
Git-diff'lenebilir. Uygulama dosyayı izler ve hot-reload eder →
**MCP olmadan bile**, sadece dosya yazan bir Claude Code oturumu çalışır.

---

## 10. Test ve Doğrulama Stratejisi

| Tür | Kapsam |
|---|---|
| **Property test** | Rastgele netlist → autoroute → **LVS geçmek ZORUNDA**. Router'ın doğruluğu makine ile kanıtlanabilir |
| Altın dosya | Router çıktıları, connectivity motoru (tohumlu RNG sayesinde stabil) |
| Birim | Union-find bağlantı motoru — en kritik bileşen, ayrı suit |
| Round-trip | doküman → kaydet → yükle → bit-birebir aynı |
| Görsel regresyon | Headless 2D/3D PNG + piksel diff |
| Perf | CI'da 3 platformda 3D benchmark: 6000 delik + 60 komponent + 200 tel @ 60 fps |
| Replay | Kaydedilmiş komut logu → aynı doküman (command bus'tan bedava) |

---

## 11. Yol Haritası

4 kişi, paralel şeritler: **A = Çekirdek** (2 kişi) · **B = UI/3D** (1) · **C = Entegrasyon** (1)

| Milestone | Süre | Kapsam | Çıkış kriteri |
|---|---|---|---|
| **M0** Risk düşürme | 2-3 hf | 3 platformda three.js stres testi · command bus + doküman iskeleti · dikey dilim | GUI'de yerleştir → MCP'den yerleştir → 3D'de gör. **Linux WebGL kararı verilmiş** |
| **M1** Editör + kütüphane | 4 hf | 2D grid editör, THT footprint kütüphanesi, parametrik gövdeler, yerleştirme, kaydet/yükle | 30 parçalı kart elle tasarlanabiliyor, 3D'de doğru görünüyor |
| **M2** Bağlantı + doğrulama | 4 hf | Union-find motoru, KiCad netlist import, ratsnest, DRC v1 (**R5' komşuluk riski dahil**), **LVS**, lehim yolu elektriksel modeli | Bilinen hatalı bir layout'ta OPEN/SHORT doğru raporlanıyor; R5' riskleri doğru işaretleniyor |
| **M3** Router + yerleştirme | 6 hf | A\*/Lee + rip-up&reroute, **lehim yolu primitifi + ray stratejisi**, SA yerleştirme, maliyet modeli ayarı, interaktif yeniden route | Property test: 100 rastgele netlist, autoroute sonrası %100 LVS geçiyor. Sürükleme < 100 ms. GND/V+ otomatik ray olarak çekiliyor |
| **M4** 3D tam | 4 hf | Montaj animasyonu, patlatılmış görünüm, yükseklik/çarpışma DRC, headless render | Rehber adımları 3D'de oynatılabiliyor, adım görselleri otomatik üretiliyor |
| **M5** Lehim rehberi | 4 hf | Sıralama motoru, adım kartları, tel kesim listesi, **doğrulama kontrol noktaları**, HTML+PDF+CSV | Gerçek bir devre bu rehberle sıfırdan lehimlenip çalıştırıldı (dogfood testi) |
| **M6** MCP + CLI sertleştirme | 2 hf | ~25 tool (gerçekleşen: 51 — §13'e bak), iki taşıma, dosya izleme, snapshot/restore | Claude Code **ve** Antigravity'den uçtan uca bir kart tasarlanıp rehber üretiliyor |
| **M7** Lansman | 3 hf | TR/EN i18n, dokümantasyon, örnek projeler, CI, paketleme, imzalama | GitHub'da yayında |

**Takvim:** paralel şeritlerle ~**5-5.5 ay** part-time. Şeritler serileşirse ~7 ay.
(D8 lehim yolu M2/M3/M5'e yayılmış ~1.5 hafta ekliyor.)

**Dogfood testi (M5) pazarlık konusu değil.** Ekipten biri rehberi takip ederek gerçek
bir kartı sıfırdan lehimlemeden M5 kapanmaz.

---

## 12. Açık Konular

**İsim.** Açık kaynak keşfedilebilirliği için isimde "perf" geçmesi değerli.
Adaylar: **PerfStudio** · **PadPilot** · **Protoforge** · **SolderPlan**
Repo: `github.com/medinstech/<isim>`

**Kod imzalama.** Windows EV sertifikası ~$300/yıl, Apple notarization $99/yıl.
Başta imzasız yayınlanabilir (SmartScreen uyarısı kabul edilir) ama planlanmalı.

**Footprint kütüphanesi kaynağı.** THT footprint'ler parametrik olarak üretilebilir
(pitch + pad çapı + drill). Alternatif: KiCad footprint'lerini atıflı bundle etmek —
CC-BY-SA-4.0 yükümlülüğü Apache-2.0 kodu bulaştırmaz ama ayrı lisanslı klasör gerekir.
**M1'de karar verilecek.**

---

## 13. Riskler

| Risk | Etki | Azaltma |
|---|---|---|
| Linux WebKitGTK'da WebGL yetersiz | Orta | M0'da ölç; platform adaptörü sayesinde Electron'a geçiş günler sürer |
| Autorouter beklenti tuzağı | **Yüksek** | "Interaktif asistan" konumlandırması; route edilemeyen netler açıkça raporlanır, sessizce bırakılmaz |
| 3D'de fotogerçekçilik scope creep | Orta | Hedef sabit: "doğru ve anlaşılır". Basit materyal, gölge bütçesi sınırlı |
| MCP tool sayısı patlaması | Orta | **Gerçekleşen: 51 tool.** Tavan tutmadı, kural tuttu: her tool `docs/MCP.md`'de bir gruba ve bir gerekçeye bağlı (11 grup; sonuncusu "tasarım", D3 notuna bak). ~25 sayısı yüzey bilinmeden atılmış bir tahmindi; korumaya çalıştığı şey sayı değil gerekçe zorunluluğuydu ve o yürürlükte. Kuralın bir kez kaydığı da ölçüldü: `reroute` sunucuda vardı, dokümanda yoktu — sayıyı üç yerde üç farklı yapan buydu, ve artık `test_mcp.py` kaymayı bir daha bırakmıyor |
| Lisans kirlenmesi (GPL'li rakip kod) | **Yüksek** | Clean-room: DIYLC/VeroRoute kaynağına bakılmayacak. Sadece striprouter (MIT) referans alınabilir |
| Kapsamın 3 kart tipine yayılması | Orta | v1 sadece pad-per-hole. Stripboard artık uçtan uca: `stripboard.py` geometri, `striproute.py` router, 2D'de kesme modu, ve `placer.py` strip hizasını skorluyor |
| **Lehim yolu güvenilirliği**: araç kullanıcıyı kırılgan yapıya teşvik edebilir | **Yüksek** | DRC bilgilendirir, engellemez: uzun saf yolda omurga önerir, FR-2'de ısı uyarısı verir, R5' risklerini test adımına çevirir. Karar kullanıcının, veri aracın |

---

## 14. Açık Kaynak Lansman Kontrol Listesi

Kutular gerçek durumu gösterir; yarısı yapılmış bir madde işaretlenmez, ne kaldığı yazılır.

- [x] README: ne işe yarar + 30 saniyelik demo GIF (`docs/images/assembly.gif`) + kurulum
- [x] Apache-2.0 LICENSE + NOTICE
- [x] CONTRIBUTING.md, CODE_OF_CONDUCT.md, issue/PR şablonları (`.github/ISSUE_TEMPLATE/`,
      "kuramadığım kart" şablonu dahil)
- [x] CI: test + lint (`ruff`) + tipler (`mypy --strict`) + 3 işletim sistemi matrisi +
      görsel regresyon (`test_render_golden.py`, suite'in içinde). 3 platform **build**'i
      `release.yml`'de, etikete basınca
- [x] Release: installer üç platformda da var (`release.yml`), Windows imzasız / macOS
      ad-hoc imzalı ama notarize değil — §12'nin kabul ettiği durum. Otomatik güncelleme
      yazıldı: uygulama günde bir kez (ve Yardım menüsünden istendiğinde) GitHub'a bakıyor,
      yeni sürümü kartın üstünde bir şeritle duyuruyor, platforma uygun dosyayı indirip
      release'e eklenen `SHA256SUMS` ile doğruluyor. **Kurulumu başlatmıyor ve bu bir
      eksik değil, karar:** imzasız bir kurulumu kullanıcı adına çalıştırmak (Windows'ta
      yükseltme, macOS'ta /Applications içindeki paketi değiştirme, Linux'ta çalışan
      AppImage'ın üstüne yazma) kötü amaçlı yazılımdan ayırt edilemeyen ve geri dönüşü
      olmayan bir mekanizma. Son tıklama kullanıcının
- [x] Örnek projeler: 555 flaşör, LM317 güç kaynağı, Arduino shield, gitar pedalı
      (`examples/`, dördü de netlist + kart)
- [x] MCP kurulum dokümanı (`docs/MCP.md`: Claude Code, ve JSON config okuyan her şey —
      Claude Desktop, Antigravity, Cursor)
- [ ] TR + EN dokümantasyon: README iki dilde ve arayüzün tam Türkçe kataloğu var.
      **Kalan:** `docs/` (MCP, RELEASING, prior-art) yalnızca İngilizce
- [ ] Duyuru: Hackaday, r/diyelectronics, r/AskElectronics, EEVblog, diyAudio, Show HN
      — M5'in dogfood testi kapanmadan yapılmaz
