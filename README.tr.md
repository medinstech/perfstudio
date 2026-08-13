# PerfStudio

[![CI](https://github.com/medinstech/perfstudio/actions/workflows/ci.yml/badge.svg)](https://github.com/medinstech/perfstudio/actions/workflows/ci.yml)
[![Lisans: Apache-2.0](https://img.shields.io/badge/lisans-Apache--2.0-blue.svg)](./LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)

[English](./README.md) · **Türkçe**

Delikli plaket (perfboard) üzerinde devre tasarlayın — tıpkı bir PCB'de yaptığınız gibi —
ve gerçekten uygulayabileceğiniz bir lehimleme rehberi alın.

![NE555 astable devresi yerleştirilmiş ve route edilmiş hâliyle 2D editör](./docs/images/editor-component-side.png)

> **Durum: pre-alpha, ve uçtan uca çalışıyor.** Netlist giriyor, lehimleme rehberi
> çıkıyor: 2D editör, 3D görünüm, yerleştirme optimizasyonu, otomatik router, DRC, LVS,
> montaj rehberi, birebir (1:1) PDF çıktısı ve bir MCP sunucusu. Eksik olan tek şey
> dogfood testi — henüz kimse üretilen bir rehberi takip ederek gerçek bir kart
> lehimlemedi ve [PLAN.md](./PLAN.md) §11'e göre bu olmadan M5 kapanmıyor. Henüz
> kurulum paketi de yok; kaynaktan çalıştırılıyor.

---

## Ne yapar

Bir şema netlist'ini alır, delik-başına-pad plaket üzerine yerleştirir, bağlantıları
router'a çözdürür, sonucun şemayla birebir örtüştüğünü kanıtlar ve ölçüm kontrol
noktaları içeren adım adım bir montaj rehberi üretir.

**Mevcut araçlardan ayrıldığı üç nokta:**

1. **Doğrulama adımları olan bir lehimleme rehberi.** Sadece "R1'i buraya lehimle" değil;
   *"blok 2 bitti → U1 pin 4 ile C3(−) arasında süreklilik olmalı"* ve *"güç vermeden
   önce: GND ile V+ arası 10 kΩ üzerinde okumalı"*. Netlist'ten türetildiği için genel
   tavsiye değil, o karta özel.
2. **Perfboard için LVS.** Kartın gerçek bağlantısallığı çıkarılır ve şemayla
   karşılaştırılır. Açık devreler, kısa devreler ve boşta kalan iletkenler havyayı elinize
   almadan önce raporlanır.
3. **Ajan-dostu.** MCP sunucusu, headless CLI ve git ile diff alınabilen proje dosyası;
   hepsi GUI ile aynı komut veri yolunu (command bus) kullanır — yani bir insanın ve bir
   modelin aynı oturumda birlikte çalıştığı bir kartta geri alma (undo) çalışır.

## Her bağlantı aynı şey değildir

Çoğu araç perfboard bağlantısını "bir tel" olarak modeller. Oysa perfboard'da iki noktayı
birleştirmenin, her birinin kendi maliyeti, sınırı ve arıza biçimi olan **altı** fiziksel
yolu vardır — ve router'ın gerçekten lehimlemesi keyifli bir yerleşim üretebilmesinin
sebebi tam olarak bu farkı modellemesidir:

| | nedir | notlar |
|---|---|---|
| bacak bükümü | komponent bacağının yakın bir deliğe bükülmesi | pratikte bedava, 3–4 delik |
| **lehim yolu** | komşu pad'lerin yalnızca lehimle birleştirilmesi | sadece dik (ortogonal); yan pad'e ~0,6 mm |
| **lehim yolu, omurgalı** | aynısı, kalaylı tel omurga üzerinde | ~10× daha düşük direnç, uzunluk sınırı yok |
| çıplak tel | lehim yüzeyinde kalaylı tel | başka bir çıplak iletkenle kesişemez |
| yalıtımlı tel | serbestçe kesişebilir | hazırlık süresi maliyeti var |
| üst jumper | komponent yüzeyinden atlayan yalıtımlı jumper | görünür, gövde alanı işgal eder |

Komşu pad'e olan 0,6 mm'lik boşluk, lehim yollarını hem bu kadar kullanışlı hem de bu
kadar kolay yanlış yapılır kılan şeydir. PerfStudio bu riski router'ın maliyet
fonksiyonuna işler ve işaretlenen her noktayı montaj rehberinde bir ölçüm adımına
dönüştürür.

## İki yüz, ve üçüncü boyut

Bakır lehim yüzeyindedir; bu yüzden lehim yüzeyi bir "ayna modu" değil, tam yetkili bir
görünümdür — ve **bakmadığınız** yüzdeki bakır taralı çizilir, çünkü kart saydam değildir
ve dolu çizilen bir iletken *bu senin önünde* der.

![Lehim yüzeyi; karşı yüzdeki bakır taralı](./docs/images/editor-solder-side.png)

3D görünüm bir resim değil, bir kontrol aracıdır. Yukarıdan bakışın hiçbir şekilde
göremeyeceği üç kural vardır: kutuya sığmayacak kadar yüksek bir parça, üzerine
lehimlenecek bir gövdenin altında sıkışan bir jumper, ve sıcak bir parçaya fazla yakın
duran ısıya duyarlı bir parça.

![Aynı kart 3D'de](./docs/images/board-3d.png)

## Çalıştırmak

**Python 3.12+** gerekir. Masaüstü uygulaması PySide6 (Qt 6) ve VTK viewport kullanır.
Henüz kurulum paketi yok — bu kaynaktan çalışan bir pre-alpha.

```sh
git clone https://github.com/medinstech/perfstudio.git
cd perfstudio
pip install -e .

perfstudio                   # boş bir kartla başlat
perfstudio bir/kart.perf     # ...ya da bir doküman aç
perfstudio --version
```

Arayüz **İngilizce ve Türkçe** konuşur (`--lang tr`, ya da sistem diline göre otomatik).

### Sıfırdan bir kart

Uygulamada: `examples/ne555-astable.net` üzerinde **File → Import KiCad Netlist**, önerilen
yerleşimi kabul edin, **Place → Auto-place Board** (`Ctrl+Shift+A`), route için **`Ctrl+R`**,
ardından **File → Export Build Guide** (`Ctrl+B`). Yukarıdaki ekran görüntüleri tam olarak
bu sıradan çıkıyor — bkz. [`tools/screenshots.py`](./tools/screenshots.py).

KiCad şart değil: netler uygulama içinde elle ya da MCP üzerinden de kurulabilir.

### Bir ajandan

MCP sunucusu birebir aynı komut veri yolunu sürer, dolayısıyla geri alma her ikisinde de
çalışır:

```sh
pip install -e ".[mcp]"
claude mcp add perfstudio -- python -m perfstudio.mcp
```

Otuz dokuz tool, ve her delik insanların perfboard'dan bahsederken kullandığı adresle
(`A1`, `C7`, `AC12`) — hiçbir yerde ham koordinat yok. Tool listesi, diğer istemcilerin
istediği JSON yapılandırması ve kurulumun geri kalanı için [docs/MCP.md](./docs/MCP.md).

### Headless

2D/3D/PDF'i dosyaya render eder, DRC ve LVS çalıştırır, süreleri yazdırır — ekran
gerekmeden. Görsel çıktının CI'da sınandığı yol budur ve bir render değişikliğinin bir
şeyi kırmadığını doğrulamanın en hızlı yoludur:

```sh
python -m perfstudio.ui.main --headless tools/diffcheck/golden/dense.perf
```

## Nasıl kurulmuş

Doküman **değişmezdir (immutable)** ve her mutasyon tek bir veri yolundan geçen bir
komuttur — insan ve ajanın karıştığı bir oturumda geri almayı çalıştıran şey budur.
Motor **saftır**: saat yok, RNG yok, dosya sistemi yok, `ui/` altında olmayan hiçbir yerde
Qt veya VTK yok. Yerleştiricinin tavlama (annealing) algoritması tohumludur (seeded);
aynı doküman ve aynı tohum aynı kartı verir.

Bu saflık süs değil, taşıyıcı bir kolondur. Buradaki Python motoru `packages/` içinde
duran TypeScript motorunun bir portudur ve kabul kriteri hiçbir zaman "testler geçiyor"
değil, "yerine geçtiği implementasyonla bayt bayt aynı sonucu üretiyor" olmuştur —
`tools/diffcheck/` altındaki golden fixture'lar, son IEEE-754 double'a kadar.

```
src/perfstudio/            motor: doküman modeli, komut veri yolu, bağlantısallık,
                           router, autorouter, yerleştirici, DRC, LVS, kalıcılık
src/perfstudio/guide.py    lehimleme rehberi; HTML/CSV/JSON için guide_export.py
src/perfstudio/parsers/    KiCad netlist içe aktarıcı
src/perfstudio/ui/         Qt uygulaması: 2D editör, VTK 3D görünüm, 1:1 PDF çıktısı
src/perfstudio/mcp/        MCP sunucusu (docs/MCP.md)
examples/                  içe aktarılacak bir netlist
tests/                     1231 test; motor mypy --strict temiz
packages/                  Python portunun karşısında kanıtlandığı referans olarak
                           saklanan orijinal TypeScript motoru
```

61 adet THT footprint **sayısal parametrelerden üretilir**, hazır varlık olarak
gelmez — mesh kütüphanesi yok, devralınacak share-alike lisansı yok. Bir parçayı 2D'de
çizen spec, 3D'de gövdesini extrude eden spec ile aynıdır; dolayısıyla ikisi birbiriyle
çelişemez.

## Nereye gidiyor

Biten: editör, kütüphane, bağlantısallık ve LVS, DRC, router ve yerleştirme
optimizasyonu, render edilmiş adım görselleri ve montaj oynatması ile montaj rehberi,
1:1 PDF çıktısı, MCP sunucusu ve TR/EN yerelleştirme.

Sırada, [PLAN.md](./PLAN.md) §11'in koyduğu sırayla:

- **Dogfood montajı (M5).** Birinin, üretilmiş bir rehberi takip ederek gerçek bir kart
  lehimlemesi gerekiyor. Bu olana kadar bu sayfadaki her iddia, çalışan bir devre
  hakkında değil, yazılım hakkında bir iddiadır.
- **Paketleme (M7).** Windows, macOS ve Linux için imzalı kurulum paketleri.
- Tek bir NE555'ten fazla örnek proje.

## Katkı

Issue ve pull request'ler memnuniyetle karşılanır. Önce [CONTRIBUTING.md](./CONTRIBUTING.md)
dosyasını okuyun — test paketinin nasıl çalıştırıldığını, hangi kontrollerin kapı olup
hangilerinin olmadığını, ve burada çoğu projeden daha çok önem taşıyan bir lisans sınırını
anlatıyor: **bu alandaki GPL lisanslı araçların kaynak kodunu okumayın ve port etmeyin.**
PerfStudio onlara karşı clean-room geliştirilmiştir ve bunun böyle kalması gerekiyor.
Kayıt [docs/prior-art.md](./docs/prior-art.md) içinde.

## Lisans

Apache-2.0. Bkz. [LICENSE](./LICENSE) ve [NOTICE](./NOTICE).
