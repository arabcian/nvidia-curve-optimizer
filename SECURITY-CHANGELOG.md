# nvcurve — Güvenlik Sertleştirme Changelog

11 dosya, +661 / -94 satır. Denetimde bulunan 13 maddenin tamamı uygulandı.

---

## Faz A — Kritik

### K-1 · SPA catch-all path traversal
**`nvcurve/server.py`** — `_resolve_under_dist()` eklendi.

`os.path.join(_dist_dir, catchall)` kaldırıldı. Artık her iki taraf da
`.resolve()` ile çözülüp `dist_root not in target.parents` kontrolünden
geçiyor.

Kapatılan saldırı: `GET /..%2f..%2f..%2fetc%2fshadow` — uvicorn yolu
routing'den önce percent-decode ettiği için `../../../etc/shadow` olarak
geliyordu ve `FileResponse` dosyayı **root yetkisiyle**, **token
istemeden** servis ediyordu.

`.resolve()` symlink'leri takip ettiği için `dist/` içine yerleştirilmiş
bir symlink de kaçış sağlayamıyor.

### K-2 · Middleware fail-open → fail-closed
**`nvcurve/server.py`** — prefix denylist yerine `_PUBLIC_EXACT` +
`_PUBLIC_PREFIXES` allowlist'i.

Eski mantık yalnızca `/api/` ve `/ws/` için token istiyordu; bu iki
prefiksin dışındaki her route sessizce korumasızdı — K-1 tam olarak bu
tuzağa düşmüştü. Artık listede olmayan her yol token istiyor, yani
**gelecekte eklenen bir route varsayılan olarak korumalı**.

Ek düzeltmeler: `Authorization` header karşılaştırması `.lower()` ile
case-insensitive; WS token'ı `parse_qs` ile percent-decode ediliyor (elle
`split("&")` decode etmiyordu).

---

## Faz B — Yüksek

### Y-1 · Daemon bind adresi doğrulaması
**`nvcurve/daemon.py`** — `_ALLOWED_BIND_HOSTS` + `trusted` parametresi.

Socket üzerinden gelen `host` artık yalnızca loopback olabilir. Root'un
`config.json`'da yapılandırdığı geniş bind ise `auto_serve` boot yolunda
`trusted=True` ile çalışıyor.

**Asimetri kasıtlı:** root `host: 0.0.0.0` yapılandırmış olsa bile, socket
istemcisi bu değeri geri yansıtarak (`{"host":"0.0.0.0"}`) bind'i
tetikleyemiyor — istenen değer `_cfg.host` ile karşılaştırılmıyor, doğrudan
reddediliyor. Aksi halde `nvcurve` grubundaki herhangi bir kullanıcı,
root'un yapılandırdığı geniş bind'i istediği anda ayağa kaldırabilirdi.

`_dispatch` içindeki varsayılan da `_cfg.host` yerine `127.0.0.1` yapıldı.
Port için tip + `1..65535` aralık kontrolü eklendi.

### Y-2 · Socket TOCTOU + kullanılmayan yetkilendirme
**`nvcurve/daemon.py`**

- Socket `umask(0o177)` altında oluşturuluyor → 0600 doğuyor, chown
  sonrası 0660'a genişliyor. Önceki kodda socket umask'e göre (tipik 0755)
  doğup ~18 satır sonra chmod ediliyordu; bu pencerede herhangi bir yerel
  kullanıcı bağlanıp komut gönderebiliyordu.
- `_allowed_uids()` eklendi; `_peer_uid()` artık gerçekten yetkilendirmede
  kullanılıyor (önce hesaplanıp yalnızca loglanıyordu). Hem `gr_mem` hem de
  primary-group üyeleri kapsanıyor.
- Stale socket unlink'i `S_ISSOCK` ile korundu — root olarak beklenmedik
  bir dosyayı silmiyor.
- İstek gövdesi için `isinstance(req, dict)` kontrolü.

### Y-3 · Guard caller'dan sink'e taşındı
**`nvcurve/hal/vfcurve.py`**, **`nvcurve/cli.py`**

`_check_point()` / `_check_delta()` eklendi; `set_mask_bit()` ve
`build_write_buffer()` (hem baştan toplu doğrulama hem paketleme sırasında)
çağırıyor.

**Neden kritik:** ctypes buffer indeksleme ve `struct.pack_into()` negatif
offset'te hata vermiyor, sessizce buffer'ın sonundan sarıyor. Doğrulandı:

```python
struct.pack_into("<i", buf, -4, 1)   # hata yok — son 4 byte'a yazar
```

Regresyon testi, `point=-5` ile version word'ünün `2024010000000000` →
`202401ff00000000` olarak bozulduğunu gösterdi. Tüm API yolları
`validate_write()` çağırıyordu ama `nvcurve setup --point` çağırmıyordu —
yani koruma tek bir unutulmuş çağrı noktası kadar inceydi.

`write_offsets()` artık `ValueError`'ı `-998` dönüş koduyla kendi
`(ret, desc)` sözleşmesine çeviriyor, böylece mevcut çağrıcılar traceback
yerine başarısız yazma görüyor. `cmd_setup`'a eksik `validate_write`
eklendi.

---

## Faz C — Orta

### O-1 · pkexec üzerinden umask kalıtımı
**`root_helper.py`**, **`nvcurve/profiles/native.py`**,
**`nvcurve/hal/snapshot.py`**, **`nvcurve/cli.py`**

pkexec — sudo'nun aksine — umask'i sıfırlamaz; çağıran GUI'den, yani
yetkisiz kullanıcıdan miras alınır. `umask 000` ile başlatılan bir GUI,
`/etc/nvcurve/profiles` dizininin **0777** doğmasına yol açıyordu.

- `root_helper.main()` başında `os.umask(0o022)`
- `_ensure_profiles_dir()` — explicit `chmod` ile
- Aynı desen `save_profile`, `snapshot.save`, `_persistent_cfg_save` için

**Not:** `os.makedirs(mode=...)` tek başına yetmez — mode da umask ile
maskelenir. Garantiyi sağlayan, oluşturma sonrası açık `chmod`.

### O-2 · profile_data şema doğrulaması
**`root_helper.py`**, **`nvcurve/safety.py`**, **`nvcurve/profiles/apply.py`**

İki ayrı boşluk kapatıldı:

1. `root_helper._validate_profile_data()` — alan allowlist'i, tip kontrolü
   (bool açıkça reddediliyor, `int`'in alt sınıfı olduğu için), aralık
   kontrolü, `curve_deltas` anahtar/değer doğrulaması. Önceden
   `profile_data` yalnızca "dict mi + 256 KB altında mı" kontrolünden
   geçip root sahipli bir dosyaya yazılıyordu — oysa `op_set_vram_memlock`
   aynı min/max değerlerini doğrudan parametre olarak aldığında titizce
   doğruluyordu. Aynı alanlar, aynı yetki, iki farklı standart.

2. `safety.validate_limits()` — ortak sürücü-aralığı doğrulayıcısı.
   `server.py`'deki inline kopya bununla değiştirildi ve
   `profiles/apply.py` artık aynı kontrolden geçiyor. Önceden bir profil
   dosyası, `/api/limits`'in uyguladığı kontrolleri tamamen atlatmanın
   yoluydu.

Reddedilen değerler warning olarak raporlanıp düşürülüyor; profilin geri
kalanı uygulanmaya devam ediyor (fonksiyonun mevcut kritik-hata/warning
ayrımıyla tutarlı).

### O-3 · Token dosyası yaşam döngüsü
**`nvcurve/server.py`**

- `O_NOFOLLOW` eklendi
- `_write_token_file()` modül gövdesinden `lifespan()` içine taşındı —
  önceden **import etmek** token dosyasını ezen bir yan etkiydi; ikinci bir
  süreç `nvcurve.server`'ı import ettiğinde (stray `uvicorn`, test, ikinci
  `serve start --direct`) çalışan sunucunun token'ını geçersiz kılıp tüm
  istemcilerini 401'e düşürüyordu
- `_remove_token_file()` shutdown'da — bayat token `/run`'da kalmıyor

### O-4 · WS token decode
`parse_qs` ile düzgün percent-decode (K-2 ile birlikte).

---

## Faz D — Sertleştirme

| # | Dosya | Değişiklik |
|---|-------|-----------|
| D-1 | `nvcurve_gui.py` | Single-instance lock `/tmp` yerine `XDG_RUNTIME_DIR` → `~/.cache/nvcurve-gui` (0700). Sabit isimli `/tmp` yolu başka kullanıcı tarafından tutulup GUI DoS edilebiliyordu. |
| D-2 | `nvcurve/cli.py`, `nvcurve/daemon.py` | Log fallback `/tmp` dışına; her iki log açılışına `O_NOFOLLOW`. |
| D-3 | `nvcurve_gui.py` | `_root_helper_is_trusted()` — pkexec öncesi uid=0 ve group/other-writable olmama kontrolü. `ROOT_HELPER_PATH` `__file__`'dan türetildiği için kaynak checkout'tan çalıştırma, kullanıcının yazabildiği bir script'i root olarak çalıştırıyordu. |
| D-5 | `install.sh` | İkon render'ı `runuser` ile normal kullanıcıda — PySide6/Qt yığınının tamamını root olarak import etmeye gerek yok. Kurulum adımı root'ta kalıyor. |
| D-6 | `nvcurve/server.py` | `lifespan()` başında `geteuid() != 0` kontrolü — `uvicorn nvcurve.server:app` CLI'yi baypas ediyordu. |
| D-7 | `nvcurve/profiles/native.py` | `list_profiles` hataları artık loglanıyor; bozuk/kurcalanmış profil izsiz kaybolmuyor. |

---

## Doğrulama

| Test | Sonuç |
|------|-------|
| `py_compile` (33 dosya) | temiz |
| `bash -n` (install/uninstall) | temiz |
| Patch pristine ağaca uygulanıyor | fuzz/offset yok |
| K-1 traversal (symlink, mutlak yol, çoklu `../`, `%2e%2e`) | 9/9 |
| K-2 middleware allowlist | 14/14 |
| O-2 şema doğrulayıcı | 19/19 |
| Y-3 sink guard + regresyon kanıtı | 17/17 |
| Y-1 trusted/untrusted bind asimetrisi | 4/4 |

Traversal testinde ilk turda bir "FAIL" çıktı (`..%2f..%2fetc/passwd`
servis edildi). Doğrulandı: uvicorn yolu routing'den **önce** decode ediyor,
yani o literal string koda hiç ulaşmıyor — test yanlıştı, kod değil. Gerçek
ASGI hattını (decode → route → resolve) simüle eden ikinci testte hepsi
geçti.

---

## Çalıştırmadan önce

**Çalışma zamanı testi yapılamadı.** Bu ortamda NVIDIA sürücüsü,
`libnvidia-api.so`, PySide6, fastapi ve NVML yok. Sözdizimi, saf mantık ve
yalıtılmış fonksiyon davranışı doğrulandı — ancak donanımda şunlar
denenmeli:

1. `nvcurve read --json` · `profile apply` · `memlock set` — Y-3 guard'ının
   normal akışı engellemediğini teyit et
2. GUI'nin Apply/Save/Reset akışları — D-3 trust kontrolü kurulu yolda
   sorunsuz geçmeli (kaynak checkout'tan çalıştırırsan **kasıtlı olarak**
   reddedecek)
3. `nvcurve serve start` + `daemon` — O-3 token'ı `lifespan`'e taşındığı
   için istemci akışını bir kez doğrula
4. `profile apply` gecikmesi — `validate_limits` artık her apply'da
   `get_power_limit` + `get_mem_offset_range` çağırıyor; NVML'de bunlar
   ucuz ama ölçmekte fayda var

**İki tasarım kararı gözden geçirilmeli:**

- `_allowed_uids()` `pwd.getpwall()` kullanıyor — LDAP/SSSD'li sistemlerde
  yavaş olabilir. Yalnızca `gr_mem` yeterliyse o bloğu çıkarabilirsin;
  primary-group üyelerini kapsamak için ekledim.
- `validate_limits` sürücü sorgusu başarısız olursa "doğrulanamıyor" olarak
  geçiyor, "geçersiz" olarak değil — aralık bildirmeyen bir GPU/sürücü
  kombinasyonunda meşru apply'ı bloklamamak için. Daha katı istersen bu
  davranışı değiştir.
