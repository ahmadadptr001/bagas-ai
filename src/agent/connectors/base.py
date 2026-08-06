"""Kerangka connector web-AI: buka halaman chat, ketik prompt, tunggu jawaban.

Satu WebConnector = satu situs (Kimi, Qwen, dst). Tiap subclass cukup mengisi
SELECTOR & URL situsnya; algoritma kirim + tunggu-jawaban ada di sini dan dibuat
TAHAN-BANTING:
  - Deteksi login KETAT: URL bukan halaman login/auth DAN kotak input terlihat
    (halaman login yang kebetulan punya elemen mirip input tak akan lolos).
  - Pembacaan jawaban memakai BEBERAPA kandidat selector + pemantauan teks
    sampai STABIL (berhenti bertambah) — bertahan walau layout situs berubah.
  - SEMUA operasi dibatasi waktu & bisa dibatalkan (cancel_event) — tak ada
    yang boleh menggantung selamanya (lihat juga timeout submit di browser.py).

Login: pertama kali dipakai, jendela Chrome TAMPIL dan pengguna sign-in manual
(termasuk CAPTCHA/2FA). Sesi disimpan permanen (persistent context), jadi
berikutnya otomatis. Semua aksi Playwright dijalankan di thread hub (browser.py).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from .. import config
from .browser import (
    BrowserError, WebBusyError, WebChatRusakError, WebKonteksPenuhError,
    WebLampiranPenuhError, WebLimitError, hub, profile_dir,
    set_windows_visible,
)

# Cari teks pendek di halaman yang cocok salah satu pola (dipakai mendeteksi
# pemberitahuan LIMIT pemakaian, mis. "You are out of free messages until ...").
#
# Dua hal penting:
#  - `exclude`: bagian PERCAKAPAN dilewati. Tanpa ini, jawaban AI yang kebetulan
#    membahas rate limit ("usage limit", dll) dikira pemberitahuan limit — dan
#    karena jawaban lama tetap ada di DOM, chat itu jadi terblokir selamanya.
#  - tidak lagi melewati elemen yang punya anak: banner limit sering dibungkus
#    markup bersarang. Yang diambil adalah kecocokan TERPENDEK (paling spesifik).
JS_FIND_TEXT = r"""
(args) => {
  // Pola ditulis untuk Python (lihat *_patterns di tiap connector), dan di
  // sana `(?i)` di depan itu lazim. JavaScript TIDAK mengenalnya: new RegExp
  // melempar "Invalid group", dan karena dulu seluruh daftar dibangun dalam
  // satu .map(), SATU pola begitu mematikan SEMUA deteksi di halaman ini —
  // senyap, karena pemanggilnya menangkap Exception lalu menjawab "tak ada".
  // TERUKUR: toast batas berkas chat.z.ai tampil 3,6 detik penuh di DOM dan
  // tetap terbaca "tak ada"; pola limit Qwen & pemicu /compact Kimi pun ikut
  // mati diam-diam karena sama-sama diawali (?i).
  // Flag 'i' memang sudah dipasang di bawah, jadi membuang penandanya aman.
  const pats = [];
  for (const p of (args.patterns || [])) {
    try {
      pats.push(new RegExp(String(p).replace(/^\(\?[a-zA-Z]+\)/, ''), 'i'));
    } catch (e) { /* pola tak sah di JS -> lewati, jangan matikan sisanya */ }
  }
  if (!pats.length) return "";
  // Jalur cepat: sekali baca teks halaman. Kalau tak ada pola yang cocok sama
  // sekali, tak perlu memeriksa elemen satu per satu (ini kasus normal).
  const body = document.body ? (document.body.innerText || '') : '';
  if (!pats.some(r => r.test(body))) return "";

  const skips = [];
  for (const sel of (args.exclude || [])) {
    try {
      for (const el of document.querySelectorAll(sel)) skips.push(el);
    } catch (e) { /* selector tak sah -> abaikan */ }
  }
  const excluded = (el) => skips.some(s => s.contains(el));

  // Semua elemen, bukan daftar tag pilihan. Dulu daftarnya dibatasi
  // div/span/p/h*/a/button, dan itu LUBANG SENYAP: bila teksnya dibungkus tag
  // lain (label, li, td, strong, atau elemen kustom milik widget captcha),
  // jalur cepat di atas cocok tapi tak ada satu pun elemen yang terpilih —
  // hasilnya "" alias "tidak ada apa-apa", padahal teksnya jelas ada di layar.
  // Perulangan ini hanya berjalan ketika jalur cepat sudah cocok (jarang),
  // jadi memindai '*' tak membebani pemakaian normal.
  let best = "";
  for (const el of document.querySelectorAll('*')) {
    const t = (el.innerText || '').trim();
    if (!t || t.length > 220) continue;
    if (excluded(el)) continue;
    if (!pats.some(r => r.test(t))) continue;
    const clean = t.replace(/\s+/g, ' ').slice(0, 180);
    if (!best || clean.length < best.length) best = clean;
  }
  return best;
}
"""

# Hitung KARTU LAMPIRAN yang sudah menempel di komposer — penanda "lampiran
# siap" yang dipakai sebelum pesan dikirim.
#
# Dua cara, sesuai apa yang diketahui connector-nya:
#   1. `card` diisi (attach_item_selector) -> hitung kartunya LANGSUNG. Ini yang
#      paling akurat, dan satu-satunya yang bekerja di situs yang menaruh
#      pratinjau DI LUAR pohon kotak input.
#   2. `card` kosong -> cara lama: hitung <img> beberapa tingkat di atas kotak
#      input. Dipertahankan sebagai cadangan untuk connector yang belum
#      memetakan kartunya (terbukti bekerja di situs yang menaruh pratinjau
#      di dalam pohon kotak input).
#
# Kenapa cara 1 perlu ada: di chat.qwen.ai cara 2 TERUKUR selalu 0 — pratinjaunya
# bukan keturunan kotak input (ditelusuri 11 tingkat ke atas pun nihil), sehingga
# penantian tak pernah terpenuhi dan unggahan dinyatakan gagal padahal file-nya
# sudah menempel.
JS_ATTACH_COUNT = r"""
(args) => {
  if (args.card) {
    try { return document.querySelectorAll(args.card).length; }
    catch (e) { return 0; }
  }
  const box = document.querySelector(args.input);
  if (!box) return 0;
  let root = box;
  for (let i = 0; i < 6 && root.parentElement; i++) root = root.parentElement;
  return root.querySelectorAll('img').length;
}
"""

StatusCb = Callable[[str], None]
TokenCb = Callable[[str], None]


# Rutin bersama yang DISISIPKAN ke tiap skrip halaman di bawah.
#
# pilihTerakhirBerisi — ambil kecocokan TERAKHIR YANG ADA ISINYA, bukan sekadar
# yang paling akhir. TERUKUR di kimi.com: `[class*='segment-assistant']` juga
# menangkap BILAH TOMBOL AKSI (`segment-assistant-actions-content`) yang teksnya
# kosong dan berada SESUDAH isi jawaban. Mengambil nodes[nodes.length-1] berarti
# memilih bilah kosong itu, sehingga jawaban terbaca "" dan dinyatakan tak ada.
#
# teksTanpaBerpikir — innerText sebuah elemen dengan BLOK BERPIKIR disembunyikan
# sementara. Sengaja MENYEMBUNYIKAN di tempat lalu memulihkannya, BUKAN memakai
# cloneNode: pada node yang TIDAK TERPASANG di dokumen, innerText merosot jadi
# semantik textContent — TERUJI di chromium, teks ber-`display:none` yang benar
# disembunyikan oleh node hidup justru IKUT TERBACA pada klon. Akibatnya label
# sr-only, tooltip, dan panel berpikir yang sedang diciutkan malah bocor ke
# jawaban — kebalikan dari tujuan penyaringan ini. Pemulihan gaya dijamin lewat
# `finally`, dan tak ada cat ulang di antaranya karena seluruhnya sinkron.
_JS_BANTU = r"""
  function pilihTerakhirBerisi(selectors) {
    for (const s of (selectors || [])) {
      let nodes;
      try { nodes = document.querySelectorAll(s); } catch (e) { continue; }
      for (let i = nodes.length - 1; i >= 0; i--) {
        if ((nodes[i].innerText || '').trim()) return nodes[i];
      }
    }
    return null;
  }
  function cocokSalahSatu(node, selectors) {
    for (const s of (selectors || [])) {
      try { if (node.matches(s)) return true; } catch (e) { /* tak sah */ }
    }
    return false;
  }
  function tersembunyi(el) {
    try {
      const g = getComputedStyle(el);
      return g.display === 'none' || g.visibility === 'hidden';
    } catch (e) { return false; }
  }
  function dalamBerpikir(node, akar, buang) {
    for (const s of (buang || [])) {
      let a;
      try { a = node.closest(s); } catch (e) { continue; }
      if (a && akar.contains(a)) return true;
    }
    return false;
  }
  function teksTanpaBerpikir(node, buang) {
    const asli = (node.innerText || '');
    if (!buang || !buang.length) return asli;
    const diubah = [];
    try {
      for (const s of buang) {
        let n;
        try { n = node.querySelectorAll(s); } catch (e) { continue; }
        for (const t of n) { diubah.push([t, t.style.display]); t.style.display = 'none'; }
      }
      if (!diubah.length) return asli;
      const bersih = (node.innerText || '');
      // PENGAMAN: bila penyaringan malah mengosongkan jawaban, berarti
      // selektornya salah menangkap seluruh jawaban -> batalkan.
      return bersih.trim() ? bersih : asli;
    } finally {
      for (const d of diubah) d[0].style.display = d[1];
    }
  }
"""

# Ambil ISI MENTAH tiap blok kode pada balasan terakhir. Dipakai untuk membaca
# usulan tool: textContent = byte apa adanya, jadi backslash & escape JSON TIDAK
# rusak oleh perenderan markdown situs (sumber bug "perintah salah path").
#
# Blok kode yang berada DI DALAM blok berpikir DILEWATI. Ini bukan kerapian:
# jalur agent memakai daftar ini sebagai CADANGAN saat usulan tool gagal dibaca
# dari teks, jadi sebuah [[TOOL]] yang cuma DIRENCANAKAN model di dalam proses
# berpikirnya — lalu diurungkan — bisa ikut terbaca dan BENAR-BENAR DIEKSEKUSI.
JS_CODE_BLOCKS = r"""
(args) => {
""" + _JS_BANTU + r"""
  const selectors = args.selectors || [];
  const buang = args.buang || [];
  // Buang GUTTER nomor baris sebelum mengambil teks: situs menampilkan nomor
  // baris sebagai elemen tersendiri di dalam <pre>, dan bila ikut terbaca
  // kodenya tampil sebagai deretan angka ("html215216217218").
  const GUTTER = /line-?number|linenos|gutter|code-?line-?no/i;
  const teks = (pre) => {
    const salinan = pre.cloneNode(true);
    for (const n of salinan.querySelectorAll('*')) {
      const cls = (typeof n.className === 'string') ? n.className : '';
      if (GUTTER.test(cls) || GUTTER.test(n.getAttribute('data-testid') || '')) {
        n.remove();
      }
    }
    const code = salinan.querySelector('code');
    return ((code ? code.textContent : salinan.textContent) || '');
  };
  const el = pilihTerakhirBerisi(selectors);
  if (!el) return [];
  const out = [];
  for (const pre of el.querySelectorAll('pre')) {
    if (buang.length && dalamBerpikir(pre, el, buang)) continue;
    out.push(teks(pre));
  }
  return out;
}
"""

# Teks jawaban TERAKHIR sebagai teks polos — dipakai untuk polling kestabilan.
#
# Seluruh pemindaian dilakukan DI HALAMAN dalam SATU panggilan. Sebelumnya
# Python menelusuri elemen satu per satu dan memanggil evaluate untuk masing
# masing, sehingga satu putaran polling (tiap 400 ms selama jawaban berlangsung)
# bisa memakan beberapa perjalanan bolak-balik CDP. Sekalian ini menghapus
# masalah handle BASI: elemen tak lagi menyeberangi batas proses, jadi DOM yang
# dirender ulang di tengah jalan tak bisa lagi menggagalkan seluruh pemindaian.
#
# `noise` = pola teks yang BUKAN jawaban (mis. indikator "Thinking…"); elemen
# yang isinya hanya itu dilewati, persis seperti pemeriksaan di Python.
JS_LAST_TEXT = r"""
(args) => {
""" + _JS_BANTU + r"""
  const selectors = args.selectors || [];
  const buang = args.buang || [];
  let noise = null;
  if (args.noise) {
    try { noise = new RegExp('^(?:' + args.noise + ')$', 's'); } catch (e) { noise = null; }
  }
  for (const s of selectors) {
    let nodes;
    try { nodes = document.querySelectorAll(s); } catch (e) { continue; }
    for (let i = nodes.length - 1; i >= 0; i--) {
      const t = teksTanpaBerpikir(nodes[i], buang).trim();
      if (!t) continue;
      if (noise && noise.test(t)) continue;
      return t;
    }
  }
  return "";
}
"""

# Serializer DOM -> Markdown (dijalankan DI HALAMAN). inner_text() membuang
# struktur (bullet, tabel, heading, blok kode) sehingga jawaban tampil polos di
# terminal; ini merekonstruksi markdown dari HTML yang sudah dirender situs agar
# rich bisa menampilkannya rapi (list, tabel, kode, bold, tautan).
JS_TO_MARKDOWN = r"""
(args) => {
""" + _JS_BANTU + r"""
  const selectors = args.selectors || [];
  const buang = args.buang || [];
  const el = pilihTerakhirBerisi(selectors);
  if (!el) return "";

  // Sentinel PATAH-BARIS-KERAS. Newline TUNGGAL di Markdown DILEBUR jadi spasi
  // oleh perender (Rich) — itulah sebab pohon direktori & baris-baris yang
  // dipisah <br> dulu tampil gepeng jadi satu paragraf. Sentinel ini ditaruh di
  // titik yang HARUS tetap patah, lalu di akhir diubah jadi "  \n" (dua spasi +
  // newline: satu-satunya patah keras yang dihormati Rich). Dipakai sentinel,
  // bukan langsung "  \n", supaya perapian di bawah tak keburu memangkas spasi
  // ekornya.
  const HB = String.fromCharCode(0xE000);

  // BLOK BERPIKIR (mode reasoning/"ahli") DILEWATI SAAT SERIALISASI — bukan
  // dibuang dari sebuah klon. Klon yang tak terpasang di dokumen membuat
  // innerText merosot jadi textContent, sehingga isi ber-`display:none` justru
  // ikut terbaca (lihat _JS_BANTU). Melewatinya di sini menjaga node tetap
  // hidup, jadi seluruh heuristik di bawah yang bersandar pada innerText —
  // termasuk ambang pembungkus blok kode — tetap memakai teks TERENDER.
  let lewati = buang;

  function listItems(listEl, ordered) {
    let out = ""; let i = 1;
    for (const li of listEl.children) {
      if (li.tagName.toLowerCase() !== "li") continue;
      const marker = ordered ? (i + ".") : "-";
      let content = ser(li).trim().replace(/\n{2,}/g, "\n").replace(/\n/g, "\n  ");
      out += marker + " " + content + "\n";
      i++;
    }
    return out;
  }
  function table(t) {
    const rows = [];
    for (const tr of t.querySelectorAll("tr")) {
      const cells = [];
      for (const c of tr.querySelectorAll("th,td"))
        cells.push((c.innerText || "").trim().replace(/\|/g, "\\|").replace(/\n/g, " "));
      if (cells.length) rows.push(cells);
    }
    if (!rows.length) return "";
    let out = "| " + rows[0].join(" | ") + " |\n";
    out += "| " + rows[0].map(() => "---").join(" | ") + " |\n";
    for (let r = 1; r < rows.length; r++) out += "| " + rows[r].join(" | ") + " |\n";
    return out + "\n";
  }
  function codeFence(pre) {
    const codeEl = pre.querySelector("code");
    let lang = "";
    if (codeEl && codeEl.className) {
      const m = codeEl.className.match(/language-([\w+-]+)/);
      if (m) lang = m[1];
    }
    // Sebagian situs tak menempelkan kelas `language-…` pada <code>, melainkan
    // menulis nama bahasanya di BILAH JUDUL blok — TERUKUR di
    // gemini.google.com: <div class="code-block-decoration"> berisi "Python".
    // Tanpa ini pagar kodenya keluar polos (```) dan pewarnaan sintaks di
    // terminal hilang, padahal informasinya ada di halaman.
    if (!lang) {
      let wrap = null;
      try { wrap = pre.closest('code-block, [class*="code-block"]'); } catch (e) {}
      const dec = wrap && wrap.querySelector(
        '[class*="code-block-decoration"], [class*="code-header"], ' +
        '[class*="code-lang"]');
      const kata = dec ? ((dec.innerText || "").trim().split(/\s+/)[0] || "") : "";
      // Bilah judul memuat tombol juga (Copy/Salin/Download) — kata seperti itu
      // BUKAN nama bahasa, dan menuliskannya sebagai bahasa justru menyesatkan.
      const BUKAN = /^(copy|salin|download|unduh|edit|run|jalankan|code|kode)$/i;
      if (/^[A-Za-z][\w+#.-]{0,14}$/.test(kata) && !BUKAN.test(kata)) {
        lang = kata.toLowerCase();
      }
    }
    // Buang gutter nomor baris; tanpa ini isi blok kode tampil sebagai
    // deretan angka saja.
    const GUTTER = /line-?number|linenos|gutter|code-?line-?no/i;
    const salinan = pre.cloneNode(true);
    for (const n of salinan.querySelectorAll("*")) {
      const cls = (typeof n.className === "string") ? n.className : "";
      if (GUTTER.test(cls) || GUTTER.test(n.getAttribute("data-testid") || "")) {
        n.remove();
      }
    }
    const inner = salinan.querySelector("code");
    const code = ((inner ? inner.textContent : salinan.textContent) || "")
                   .replace(/\n$/, "");
    return "\n```" + lang + "\n" + code + "\n```\n\n";
  }
  function ser(node) {
    let out = "";
    for (const ch of node.childNodes) {
      if (ch.nodeType === 3) { out += ch.textContent; continue; }
      if (ch.nodeType !== 1) continue;
      const tag = ch.tagName.toLowerCase();
      // Buang chrome UI (tombol salin/svg) yang bukan isi jawaban.
      if (tag === "button" || tag === "svg") continue;
      // Lewati blok berpikir beserta seluruh isinya.
      if (lewati.length && cocokSalahSatu(ch, lewati)) continue;
      // Lewati yang TIDAK TERENDER (display:none / visibility:hidden).
      //
      // Serializer ini memungut simpul teks lewat textContent, yang tak peduli
      // CSS — jadi tanpa pemeriksaan ini teks sr-only, tooltip, dan panel yang
      // sedang diciutkan ikut masuk ke jawaban. Jalur teks polos memakai
      // innerText dan sudah membuangnya sejak dulu, sehingga dua jalur
      // pembacaan yang sama bisa memberi jawaban BERBEDA untuk balasan yang
      // sama. Aman dilakukan di sini karena node-nya HIDUP (tak diklon):
      // getComputedStyle pada node terlepas tak menghasilkan apa-apa.
      if (ch.nodeType === 1 && tersembunyi(ch)) continue;
      if (/^h[1-6]$/.test(tag)) {
        out += "\n" + "#".repeat(+tag[1]) + " " + (ch.innerText || "").trim() + "\n\n";
      } else if (tag === "p") {
        out += ser(ch).trim() + "\n\n";
      } else if (tag === "br") {
        // <br> = patah baris yang DISENGAJA penulis. Sebagai newline tunggal ia
        // dilebur Rich jadi spasi; pakai patah keras agar benar-benar patah.
        out += HB;
      } else if (tag === "strong" || tag === "b") {
        out += "**" + ser(ch).trim() + "**";
      } else if (tag === "em" || tag === "i") {
        out += "*" + ser(ch).trim() + "*";
      } else if (tag === "del" || tag === "s") {
        out += "~~" + ser(ch).trim() + "~~";
      } else if (tag === "pre") {
        out += codeFence(ch);
      } else if (tag === "code") {
        out += "`" + ch.textContent + "`";
      } else if (tag === "ul") {
        out += "\n" + listItems(ch, false) + "\n";
      } else if (tag === "ol") {
        out += "\n" + listItems(ch, true) + "\n";
      } else if (tag === "blockquote") {
        const inner = ser(ch).trim();
        out += inner.split("\n").map(l => "> " + l).join("\n") + "\n\n";
      } else if (tag === "a") {
        const href = ch.getAttribute("href") || "";
        const txt = ser(ch).trim();
        out += href ? ("[" + txt + "](" + href + ")") : txt;
      } else if (tag === "table") {
        out += "\n" + table(ch);
      } else if (tag === "hr") {
        out += "\n---\n\n";
      } else if (tag === "li") {
        out += ser(ch);
      } else {
        // PEMBUNGKUS BER-BILAH-ALAT: <div> yang isinya cuma SATU blok (<pre>
        // atau <table>) ditambah bilah kecil berisi label + tombol salin.
        // Bilah itu bukan bagian jawaban, jadi emit bloknya saja — kalau tidak,
        // labelnya bocor jadi teks. TERUKUR di kimi.com: blok kode dibungkus
        // dengan label bahasa + "Copy", dan tabel dibungkus
        // `div.table.markdown-table` berisi "Table\nCopy\n" (11 karakter) di
        // atas tabelnya.
        //
        // Syarat "tak ada elemen prosa LAIN" WAJIB ada di samping ambang 40
        // karakter. TERUJI: wadah jawaban yang isinya kalimat PENDEK + satu blok
        // (mis. "Selesai, ini hasilnya:" lalu kodenya — bentuk paling lazim di
        // jalur agent) ikut lolos ambang itu, sehingga kalimatnya DIBUANG dan
        // yang tampil cuma bloknya. Pembungkus sungguhan tak pernah memuat
        // <p>/<ul>/heading, jadi syarat ini tak melemahkannya.
        const bungkusPendek = (sel, prosaLain) => {
          const inti = ch.querySelector ? ch.querySelector(sel) : null;
          if (!inti) return null;
          if ((ch.innerText || "").length - (inti.innerText || "").length >= 40) {
            return null;
          }
          return ch.querySelector(prosaLain) ? null : inti;
        };
        const pre = bungkusPendek(
          "pre", "p,ul,ol,h1,h2,h3,h4,h5,h6,table,blockquote");
        if (pre) { out += codeFence(pre); continue; }
        const tabel = bungkusPendek(
          "table", "p,ul,ol,h1,h2,h3,h4,h5,h6,pre,blockquote");
        if (tabel) { out += "\n" + table(tabel); continue; }
        const dalam = ser(ch);
        out += dalam;
        // Elemen BLOK generik (div/section, mis. tiap baris pohon direktori yang
        // dirender sebagai <div> tersendiri) dulu digabung TANPA pemisah apa pun
        // -> semuanya berdempet di satu baris. Beri patah keras bila isinya teks
        // sebaris yang belum menutup bloknya sendiri. Elemen inline (span) tak
        // disentuh, dan wadah yang isinya sudah berakhir newline (mis. berisi
        // <p>) juga dilewati agar tak menambah baris kosong.
        let disp = "";
        try { disp = getComputedStyle(ch).display || ""; } catch (e) {}
        const blok = disp && !disp.startsWith("inline") &&
                     disp !== "contents" && disp !== "none";
        if (blok && dalam.trim() && !/\n\s*$/.test(dalam) &&
            dalam[dalam.length - 1] !== HB) {
          out += HB;
        }
      }
    }
    return out;
  }
  const rapikan = (s) => {
    // Perapian jalan LEBIH DULU selagi patah keras masih berupa sentinel (bukan
    // spasi/newline), jadi pemangkasan spasi-ekor di bawah tak menyentuhnya.
    s = s.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n");
    // Sentinel -> patah keras Markdown (dua spasi + newline). Spasi di sekitarnya
    // diserap supaya tak jadi indentasi liar di baris berikutnya.
    s = s.replace(new RegExp("[ \\t]*" + HB + "[ \\t]*", "g"), "  \n");
    s = s.replace(/(?:  \n){2,}/g, "  \n");   // patah beruntun -> satu
    s = s.replace(/  \n(\n)/g, "$1");          // patah tepat sebelum paragraf: buang
    return s.replace(/[ \t]+$/g, "").trim();
  };
  let hasil = rapikan(ser(el));
  // PENGAMAN: selektor berpikir yang salah menangkap SELURUH jawaban akan
  // menyisakan hasil kosong -> ulangi tanpa penyaringan, lebih baik ada blok
  // berpikir yang bocor daripada jawaban hilang sama sekali.
  if (!hasil && lewati.length) {
    lewati = [];
    hasil = rapikan(ser(el));
  }
  return hasil;
}
"""


class WebConnector:
    """Basis connector. Subclass mengisi atribut kelas di bawah."""

    service: str = ""          # kunci internal & nama folder profil (mis. "kimi")
    label: str = ""            # nama tampilan (mis. "Kimi (web)")
    chat_url: str = ""         # halaman chat / sesi baru
    # Tombol "chat baru" milik situs. Bila diisi, memulai percakapan baru cukup
    # MENGEKLIKNYA alih-alih memuat ulang seluruh SPA — jauh lebih cepat dan tak
    # membuang sesi yang sudah hangat. Kosong = selalu lewat navigasi URL.
    # Kegagalan tak fatal: pemanggil jatuh ke navigasi biasa.
    #
    # Boleh SATU selector (str) atau BEBERAPA kandidat (tuple) yang dicoba
    # BERURUTAN. Pakai tuple bila ada lebih dari satu kandidat — TERBUKTI di
    # kimi.com: `[aria-label="New Chat"]` ternyata menempel pada LOGO situs, dan
    # dalam satu daftar berkoma `.first` mengambil elemen paling awal di DOM
    # (bukan yang paling spesifik), sehingga yang terklik bisa logo, bukan
    # tombolnya.
    new_chat_selector: str | tuple[str, ...] = ""
    # Kotak input (textarea / contenteditable). Boleh SATU selector (str) atau
    # BEBERAPA kandidat (tuple) yang dicoba BERURUTAN — kandidat pertama yang
    # benar-benar terlihat & bisa diisi yang dipakai.
    #
    # Sengaja tuple, bukan satu string berkoma: daftar CSS berkoma TIDAK
    # menentukan prioritas (yang terpilih adalah elemen paling awal di DOM,
    # bukan yang paling spesifik), dan memecahnya sendiri dengan split(",")
    # merusak selector yang memuat koma di dalam kutip/kurung.
    input_selector: str | tuple[str, ...] = ""
    # Wadah pesan JAWABAN — boleh SATU selector (str) atau BEBERAPA kandidat
    # (tuple); dicoba berurutan, yang pertama menghasilkan teks dipakai.
    message_selector: str | tuple[str, ...] = ""
    input_is_contenteditable: bool = False
    submit_key: str = "Enter"  # tombol kirim
    # Tombol KIRIM di komposer, dipakai sebagai CADANGAN bila Enter ternyata tak
    # men-submit. Itu bukan kemungkinan teoretis: di chat.qwen.ai dengan lampiran
    # TERUKUR Enter tak mengirim apa pun — prompt tertinggal di kotak, lalu
    # giliran gagal dengan "balasan tak terdeteksi" yang menyesatkan (seolah
    # selector jawabannya yang salah, padahal pesannya belum pernah berangkat).
    send_button_selector: str = ""
    # True = jawaban FINAL direkonstruksi jadi Markdown dari HTML (agar list,
    # tabel, heading, blok kode tampil rapi di terminal), bukan teks polos.
    read_as_markdown: bool = False
    # Penanda URL halaman LOGIN/AUTH: selama URL page mengandung salah satu ini,
    # user pasti BELUM login — jangan pernah dicap "siap" walau ada elemen input
    # yang kebetulan cocok selector (inilah sumber salah-deteksi sebelumnya).
    login_url_markers: tuple[str, ...] = (
        "login", "signin", "sign-in", "sign_in", "oauth", "/auth", "sso",
    )
    # Selector yang HANYA ada saat BELUM login (mis. tombol "Log in"). Sebagian
    # situs (chat.qwen.ai) menampilkan kotak input untuk TAMU di URL chat biasa,
    # jadi "input terlihat" BUKAN bukti sudah masuk — tanpa penanda ini bagas-ai
    # mengirim pesan yang tak pernah diproses lalu menunggu jawaban hampa.
    logged_out_selector: str = ""
    # Selector yang HANYA ada saat SUDAH login (mis. tombol profil akun).
    #
    # BUKTI POSITIF, dan itu jauh lebih kuat daripada sekadar "tak ada tombol
    # Log in". TERUKUR di kimi.com: sesudah goto, kotak input sudah terlihat
    # pada detik 6,9 sementara tombol "Log In" baru dirender pada detik 11,1 —
    # ADA JENDELA 4,2 DETIK di mana halaman TAMU lolos sebagai "sudah login",
    # lalu prompt diketik ke kotak tamu, dikirim, dan tak pernah diproses.
    #
    # Menunggu load-state BUKAN jalan keluarnya: pada situs yang sama
    # `networkidle` baru tiba di detik 31,3 dan `load` malah kehabisan waktu 30
    # detik — terlalu mahal untuk dipasang di tiap navigasi.
    #
    # Kosong = connector tak berpendapat, perilakunya sama seperti sebelumnya.
    logged_in_selector: str = ""
    # Tombol penutup banner/dialog yang bisa MENGHALANGI komposer
    # (persetujuan cookie, tur fitur, promo). Kosong = situs ini tak punya,
    # dan pemeriksaannya dilewati sepenuhnya. Lihat _tutup_penghalang.
    dismiss_selectors: tuple[str, ...] = ()
    # Selector penanda "sedang mengetik/streaming" (bila situs punya).
    streaming_selector: str = ""
    # Pola teks pemberitahuan LIMIT pemakaian di situs. Sering baru MUNCUL
    # setelah prompt dikirim — tanpa deteksi ini bagas-ai menunggu jawaban yang
    # memang tak akan datang, lalu gagal dengan pesan yang membingungkan.
    limit_patterns: tuple[str, ...] = ()
    # Pola teks SPANDUK GALAT situs (bukan soal kuota): permintaan ditolak,
    # koneksi ke model gagal, percakapan rusak. Hanya dibaca di jalur "balasan
    # tak pernah muncul" — lihat _raise_if_error.
    error_patterns: tuple[str, ...] = ()
    # Pola teks "percakapan ini sudah KEPANJANGAN, mulai sesi baru". Beda dari
    # ketiganya di atas: kuota aman, chat tidak rusak, situs juga tidak menolak —
    # ia cuma memberi tahu bahwa konteksnya sudah mentok. Ditangani dengan
    # MERINGKAS lalu pindah ke chat baru (lihat Agent._padatkan_web); karena itu
    # ia perlu jenisnya sendiri, bukan digabung ke error_patterns.
    context_full_patterns: tuple[str, ...] = ()
    # Bagian halaman yang TIDAK boleh ikut dipindai saat mencari pemberitahuan
    # limit — biasanya wadah pesan percakapan, karena jawaban AI sendiri bisa
    # membahas "rate limit" dan itu bukan tanda kuota habis.
    limit_exclude_selectors: tuple[str, ...] = ()
    # Jarak minimal antar-pemeriksaan limit (detik). Pemeriksaan memindai DOM,
    # jadi jangan dijalankan tiap putaran polling (300 ms).
    limit_poll_seconds: float = 2.0
    # Pola teks "server sedang KEWALAHAN" — beda dari limit_patterns: kuota kita
    # aman, servernya yang penuh, dan biasanya pulih dalam hitungan detik.
    # Pemberitahuannya menggantikan isi balasan, jadi tanpa deteksi ini ia tampil
    # sebagai "jawaban" model.
    busy_patterns: tuple[str, ...] = ()
    # Pemberitahuan sibuk selalu PENDEK. Ambang ini penjaga salah-tangkap yang
    # penting: jawaban model yang KEBETULAN membahas server sibuk hampir pasti
    # jauh lebih panjang, jadi teks panjang tak pernah dianggap pemberitahuan.
    # (Pelajaran dari connector claude.ai yang kini dihapus: pola longgar pernah
    # membatalkan giliran yang sebenarnya normal.)
    busy_max_chars: int = 400
    # Input file untuk MELAMPIRKAN gambar (mis. screenshot) ke pesan. Kosong =
    # situs ini tak mendukung lampiran.
    file_input_selector: str = ""
    # KARTU pratinjau lampiran di komposer — penanda "file sudah benar-benar
    # menempel". Bila diisi, inilah yang dihitung; kosong = pakai cara lama
    # (menghitung <img> di sekitar kotak input). Sebisanya pilih selector yang
    # MENGECUALIKAN kartu yang masih mengunggah (mis. `:not(.loading)`): kartunya
    # muncul seketika, dan mengirim saat itu berarti pesan berangkat sebelum
    # gambarnya selesai terunggah.
    attach_item_selector: str = ""
    # Batas waktu menunggu unggahan selesai (detik).
    attach_timeout: float = 90.0
    # Pola teks "jumlah berkas per percakapan sudah mentok" (mis. chat.z.ai:
    # "You can only chat with a maximum of 10 file(s) at a time."). Beda dari
    # limit_patterns: kuota pemakaian aman, yang penuh cuma daftar lampiran —
    # dan pesannya masih bisa dikirim tanpa gambar. Kosong = situs ini belum
    # pernah terlihat membatasi.
    attach_limit_patterns: tuple[str, ...] = ()
    # CAPTCHA / verifikasi keamanan yang muncul di tengah jalan. Situs
    # memasangnya tanpa aba-aba, dan selama ia tampil komposer terkunci &
    # jawaban tak pernah datang — gejalanya di terminal tampak seperti dua
    # kerusakan lain sekaligus ("komposer menolak Enter", "tak ada balasan
    # baru"). Satu-satunya jalan keluar adalah TANGAN MANUSIA, jadi yang bisa
    # dilakukan bagas-ai cuma mengenalinya lalu membukakan jendelanya.
    captcha_patterns: tuple[str, ...] = ()
    # Elemen kotak captchanya, bila bentuknya sudah diketahui. Ini penanda yang
    # jauh lebih kokoh daripada teks: teks bisa berganti bahasa (situsnya
    # Tiongkok — kalimatnya sewaktu-waktu bisa jadi Mandarin), berganti kata,
    # atau digambar di canvas. Elemennya tidak. TERUKUR di halaman pengguna:
    # kotak Aliyun Captcha punya id tetap dan tinggal diperiksa terlihat atau
    # tidak. Yang diperiksa VISIBILITASNYA, bukan keberadaannya — widgetnya
    # menyisakan elemennya di DOM (display:none) sesudah tekaannya selesai.
    captcha_selectors: tuple[str, ...] = ()
    # Tombol BATAL pada tiap kartu lampiran di komposer (silang kecilnya).
    # Dipakai saat unggahan gagal/ditolak: kartu yang terlanjur menempel harus
    # dilepas, kalau tidak pesan pengganti yang dikirim menyusul ikut membawa
    # (atau ikut diblokir oleh) lampiran yang sama. Kosong = connector ini
    # belum dipetakan tombol batalnya.
    attach_clear_selector: str = ""
    # Teks yang BUKAN jawaban (chrome UI situs), mis. indikator berpikir
    # "Thought for 2s". Bila SELURUH teks yang terbaca hanya ini, artinya jawaban
    # BELUM muncul — jangan dianggap sebagai balasan (akar bug: giliran berhenti
    # lebih awal & mengembalikan "Thought for 2s" alih-alih jawaban asli).
    noise_pattern: str = ""
    # Wadah BLOK BERPIKIR (mode reasoning/"ahli"). Sebagian situs menaruh proses
    # berpikirnya DI DALAM wadah jawaban, jadi tanpa ini ia ikut terbaca sebagai
    # bagian jawaban — dan pada jalur agent, isinya bisa memuat blok [[TOOL]]
    # yang cuma DIRENCANAKAN lalu ikut dieksekusi. Menargetkan CLASS/atribut
    # (bukan teks) agar jawaban biasa tak salah terbuang; pembuangan yang malah
    # mengosongkan jawaban DIBATALKAN otomatis (lihat JS_TO_MARKDOWN & _el_text).
    strip_selectors: tuple[str, ...] = ()

    # --- Aksi UI yang bisa DIKLIK program di situs (permintaan pengguna: ganti
    #     varian model & mode berpikir dari terminal via /effort). Tiap aksi =
    #     (label tampil, urutan teks yang diklik berurutan, deskripsi). Urutan
    #     >1 elemen dipakai untuk menu bertingkat (mis. buka "Effort" lalu "High").
    # Tombol pembuka menu BAWAAN (dipakai bila aksi tak menyebut tombolnya
    # sendiri). Situs bisa punya LEBIH DARI SATU kontrol — mis. chat.qwen.ai
    # menaruh pemilih model di ATAS dan pemilih mode di dekat kotak input —
    # jadi tiap aksi boleh menentukan tombol pembukanya sendiri (elemen ke-4).
    web_model_button: str = ""
    # (label, urutan teks yang diklik, keterangan[, selector tombol pembuka])
    web_actions: tuple[tuple, ...] = ()
    # MODE KERJA situs — beda maksud dari web_actions, jadi sengaja dipisah.
    #
    #   web_actions = varian MODEL & usaha berpikir (dipakai /effort);
    #   web_modes   = tombol yang mengubah APA yang dihasilkan situs —
    #                 "Create Image", "Create Video", "Deep Search", dsb.
    #
    # Dipisah karena keduanya menjawab pertanyaan pengguna yang berbeda ("model
    # mana?" vs "mau bikin apa?"), dan mencampurnya jadi satu menu membuat
    # pengguna harus memilah sendiri mana yang mana.
    #
    # Bentuk tiap entri sama persis dengan web_actions:
    #   (label, (teks yang diklik berurutan…), deskripsi, tombol_pembuka)
    # `tombol_pembuka` kosong = tombolnya LANGSUNG ada di komposer, tak perlu
    # membuka menu apa pun lebih dulu.
    web_modes: tuple[tuple, ...] = ()
    # Selector tombol MEMATIKAN mode kerja (kembali ke chat biasa). Kosong =
    # belum dipetakan; /mode tak menawarkannya, dan pengguna diberi tahu bahwa
    # jalan pulangnya adalah memulai chat baru.
    web_mode_off_selector: str = ""
    # Kandidat selector ITEM MENU — dipakai untuk memastikan menu benar-benar
    # terbuka DAN untuk mengeklik pilihannya. Bawaannya pola ARIA yang lazim.
    #
    # Wajib bisa ditimpa: TERUKUR di kimi.com, `[role="menuitem"]`,
    # `[role="menuitemradio"]`, dan `[role="option"]` semuanya NOL — menunya
    # tersusun dari <div> biasa (.model-item, .effort-option). Dengan daftar
    # ARIA yang dipatok mati, menu situs seperti itu selalu dianggap "tak mau
    # terbuka" dan /effort mustahil bekerja di sana.
    #
    # Tuple, bukan string berkoma, dengan alasan yang sama seperti
    # input_selector: kandidat harus dicoba BERURUTAN menurut kekhususannya.
    menu_item_selector: str | tuple[str, ...] = (
        '[role="menuitemradio"]', '[role="menuitem"]', '[role="option"]',
    )
    # Selector tombol "berhenti" yang HANYA ada selagi situs menjawab. Penanda
    # paling andal bahwa balasan masih berjalan.
    stop_selectors: tuple[str, ...] = ()

    # Biarkan jendela browser TERLIHAT, jangan disembunyikan ke latar. Dipakai
    # untuk situs yang prosesnya memang ingin DIAMATI langsung — mis. Kimi, yang
    # menampilkan langkah berpikir & pencarian web selagi menjawab, dan itu
    # tak terlihat sama sekali kalau jendelanya disembunyikan.
    #
    # Bisa dipaksa untuk SEMUA situs lewat CONNECTOR_SHOW=true di .env.
    show_window: bool = False

    # Ukuran layout MINIMAL yang dijamin untuk situs ini (piksel CSS). 0 = tak
    # dipedulikan, perilakunya persis seperti sebelum ini ada.
    #
    # Kenapa perlu: context diluncurkan dengan no_viewport=True, jadi layout
    # situs mengikuti UKURAN JENDELA Chrome yang sesungguhnya — yang bergantung
    # pada resolusi layar pengguna dan apakah jendelanya termaksimalkan. Situs
    # chat modern responsif, dan pada lebar kecil sebagian kontrol tidak sekadar
    # bergeser melainkan HILANG dari DOM. TERUKUR di gemini.google.com: pemilih
    # model masih ada pada 600 px dan lenyap pada 430 px, sehingga /effort
    # mustahil bekerja di layar sempit tanpa ada yang salah pada kodenya.
    #
    # Yang dipasang bukan viewport Playwright (tak berlaku pada no_viewport)
    # melainkan penggantian metrik lewat CDP — lihat _normalize_layout.
    min_layout_width: int = 0
    min_layout_height: int = 0

    # Batas waktu (detik).
    login_timeout: float = 300.0     # tunggu pengguna menyelesaikan login
    answer_timeout: float = 300.0    # tunggu jawaban selesai
    start_timeout: float = 90.0      # tunggu jawaban MULAI muncul
    # Berapa kali cek berturut-turut teks tak berubah -> dianggap selesai.
    _stable_needed: int = 5
    _poll_ms: int = 400
    # Batas anti-"mengoceh": panjang balasan yang, bila dilewati, generasi
    # dihentikan paksa. Model kadang terjebak mengulang potongan yang sama tanpa
    # henti (terukur di Kimi: ~108 rb token / 6 menit menulis baris identik).
    # ~150 rb karakter ≈ 37 rb token — sangat sedikit balasan sah melampauinya,
    # sedangkan kasus mengoceh membengkak jauh di atasnya.
    _MAX_REPLY_CHARS: int = 150_000
    # Tenggat menunggu halaman siap SESUDAH bernavigasi. Sengaja jauh lebih
    # longgar daripada jalur cepat (1,5 detik untuk halaman yang sudah terbuka):
    # SPA butuh waktu boot. TERUKUR di kimi.com pada sesi yang SUDAH LOGIN —
    # kotak input terlihat pada 0,11 detik, tetapi bukti-positif login
    # (.user-profile-trigger) baru pada 3,02 detik. Dengan tenggat yang terlalu
    # ketat, sesi yang sehat justru divonis "belum login" lalu jendela login
    # muncul tanpa perlu. 15 detik memberi kelonggaran ~5x dari yang terukur,
    # dan hanya benar-benar terpakai saat halaman memang belum siap.
    _NAV_READY_MS: int = 15000

    # Pola menangkap ID percakapan dari URL (mis. kimi.com/chat/<id>).
    chat_id_pattern: str = r"/chat/([0-9a-fA-F-]{16,})"
    # Template URL untuk MEMBUKA percakapan lama (lanjut chat yang sudah ada).
    chat_url_template: str = ""
    # ID percakapan yang terakhir dipakai (diisi tiap kali send selesai).
    last_chat_id: str = ""
    # Isi mentah blok kode balasan terakhir (diisi tiap kali send selesai).
    last_code_blocks: list[str] = []
    # True bila situs MEMPERINGATKAN percakapan ini sudah kepanjangan pada kirim
    # terakhir. Sengaja bendera, bukan galat: peringatannya datang bersama
    # balasan yang berhasil, jadi melemparnya akan membuang pekerjaan yang
    # barusan selesai (lihat _padatkan_web di core.py).
    konteks_penuh: bool = False

    def chat_url_for(self, chat_id: str) -> str:
        """URL percakapan lama. "" bila situs ini tak mendukung."""
        if not (self.chat_url_template and chat_id):
            return ""
        return self.chat_url_template.format(id=chat_id)

    def supports_resume(self) -> bool:
        """True bila percakapan lama bisa dibuka lagi (untuk --resume)."""
        return bool(self.chat_url_template)

    # ---- catatan chat yang DIBUAT bagas-ai (agar penghapusan aman) ----
    def _registry_path(self):
        """File catatan chat buatan bagas-ai untuk service ini."""
        return config.CONFIG_HOME / "browser" / f"{self.service}_chats.json"

    def own_chats(self) -> list[dict]:
        """Daftar chat yang DIBUAT bagas-ai (terbaru dulu). Dipakai agar fitur
        bersih-bersih tak pernah menyentuh percakapan pribadi pengguna."""
        try:
            data = json.loads(self._registry_path().read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_own_chats(self, rows: list[dict]) -> None:
        p = self._registry_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(rows[:500], ensure_ascii=False),
                         encoding="utf-8")
        except OSError:
            pass

    def record_chat(self, chat_id: str, title: str = "") -> None:
        """Catat satu chat baru buatan bagas-ai."""
        if not chat_id:
            return
        rows = [r for r in self.own_chats() if r.get("id") != chat_id]
        rows.insert(0, {"id": chat_id, "title": (title or "")[:80],
                        "ts": time.time()})
        self._save_own_chats(rows)

    def forget_chats(self, ids: set[str]) -> None:
        """Buang beberapa chat dari catatan (setelah dihapus di situs)."""
        self._save_own_chats([r for r in self.own_chats()
                              if r.get("id") not in ids])

    def current_chat_id(self, page: Any) -> str:
        """ID percakapan yang sedang terbuka (dari URL), "" bila tak dikenali."""
        try:
            m = re.search(self.chat_id_pattern, page.url or "")
            return m.group(1) if m else ""
        except Exception:  # noqa: BLE001
            return ""

    # ---- API publik ----
    def supports_chat_admin(self) -> bool:
        """True bila connector ini bisa mendaftar & menghapus chat di situs."""
        return False

    def list_chats(self) -> list[dict]:
        """Semua percakapan di akun situs: [{id, title, created, updated}]."""
        raise BrowserError(
            f"{self.label} belum mendukung pengelolaan chat dari bagas-ai.")

    def delete_chats(self, ids: list[str]) -> int:
        """Hapus percakapan berdasarkan ID; kembalikan jumlah yang terhapus."""
        raise BrowserError(
            f"{self.label} belum mendukung penghapusan chat dari bagas-ai.")

    def prune_own_chats(self, keep: int) -> int:
        """Hapus chat LAMA buatan bagas-ai, sisakan `keep` yang terbaru.
        Hanya menyentuh chat yang tercatat dibuat bagas-ai."""
        if keep < 0 or not self.supports_chat_admin():
            return 0
        rows = self.own_chats()
        extra = rows[keep:]
        if not extra:
            return 0
        ids = [r["id"] for r in extra if r.get("id")]
        try:
            n = self.delete_chats(ids)
        except BrowserError:
            return 0
        self.forget_chats(set(ids))
        return n

    def connect(
        self,
        *,
        on_status: StatusCb | None = None,
        cancel_event: Any = None,
    ) -> bool:
        """Hubungkan ke situs — dipanggil SAAT MODEL DIPILIH (/model), bukan saat
        pesan pertama. Belum pernah login -> diarahkan ke Chrome untuk login
        SEKALI; sudah pernah -> langsung tersambung ke sesi chat.

        Return True bila proses login baru saja dilakukan (False = sesi lama)."""
        return hub().submit(
            lambda h: self._connect_on_hub(h, on_status, cancel_event),
            timeout=self.login_timeout + 90,
        )

    def send(
        self,
        prompt: str,
        *,
        on_status: StatusCb | None = None,
        on_token: TokenCb | None = None,
        cancel_event: Any = None,
        new_chat: bool = False,
        open_chat_id: str = "",
        complete_when: Callable[[str], bool] | None = None,
        attachments: list[str] | None = None,
        on_notice: Callable[[str], None] | None = None,
    ) -> str:
        """Kirim prompt ke situs & kembalikan teks jawaban (lewat thread hub).

        `attachments` = daftar path file (mis. screenshot) yang DILAMPIRKAN ke
        pesan ini, sehingga AI web bisa benar-benar MELIHAT gambarnya.

        `new_chat=True` memulai PERCAKAPAN BARU di situs (buang konteks chat lama)
        — dipakai pada pesan pertama tiap sesi bagas-ai supaya AI web tak terbawa
        konteks percakapan sebelumnya.

        `open_chat_id` = LANJUTKAN percakapan lama dengan ID itu (dipakai saat
        --resume / memilih sesi dari menu), sehingga konteks proyek yang sudah
        dikirim di chat tersebut tak perlu diulang.

        `complete_when(teks)` (opsional) = syarat TAMBAHAN bahwa balasan sudah
        utuh. Dipakai pemanggil untuk menahan kesimpulan 'selesai' saat balasan
        masih setengah dirender (mis. blok usulan tool belum tertutup).

        PULIH SEKALI bila tab-nya mati sebelum sempat mengalirkan teks apa pun.
        Kematian tab itu terjadi sesekali & acak (prosesnya Chrome tetap hidup,
        hanya tab-nya hilang), dan tanpa pemulihan ia muncul ke pengguna sebagai
        kegagalan penuh. Sengaja HANYA bila BELUM ada teks yang diteruskan ke
        on_token: kalau jawaban sudah separuh tampil di terminal, mengulang
        berarti mencetaknya dua kali — lebih baik gagal jujur & biarkan pengguna
        mengirim ulang."""
        mengalir = False
        _token: TokenCb | None = None
        if on_token is not None:
            def _token(chunk: str) -> None:      # noqa: F811 - hanya bila dipakai
                nonlocal mengalir
                mengalir = True
                on_token(chunk)

        def _antre() -> None:
            if on_status:
                on_status("menunggu giliran browser sebelumnya selesai…")

        # Saluran KABAR dititipkan lewat atribut, bukan argumen: ia cuma dipakai
        # penanganan captcha, dan menambah satu parameter lagi ke rantai
        # send -> _send_on_hub -> _submit hanya untuk itu membuat tiga tanda
        # tangan fungsi ikut melar tanpa alasan.
        #
        # PENTING: on_status TIDAK cukup. Terminal meringkas status connector
        # jadi SATU KATA fase (cli._web_phase), jadi kalimat "selesaikan captcha
        # di jendela" hilang seluruhnya sebelum sampai ke mata pengguna —
        # pemberitahuan yang tak pernah terbaca sama saja dengan tak ada.
        self._kabar = on_notice

        def _sekali() -> str:
            return hub().submit(
                lambda h: self._send_on_hub(
                    h, prompt, on_status, _token, cancel_event, new_chat,
                    complete_when, open_chat_id, list(attachments or [])),
                timeout=self.login_timeout + self.answer_timeout
                + (self.attach_timeout if attachments else 0) + 120,
                on_wait=_antre,
            )

        try:
            return _sekali()
        except Exception as exc:  # noqa: BLE001 - hanya kematian tab yang diulang
            if mengalir or not self._is_dead_target(exc):
                raise
        if on_status:
            on_status(f"tab {self.label} mati, mengulang…")
        # Context zombie-nya TIDAK dibuang di sini: hub.page_for sudah memeriksa
        # halaman masih hidup atau tidak, lalu membuang & meluncurkan ulang
        # sendiri. Membuang manual di sini hanya menambah satu siklus
        # bunuh-luncur di atas yang sudah dilakukan _acquire_ready_page.
        return _sekali()

    def set_web_option(self, label: str) -> str:
        """Klik OPSI di UI web (varian model / mode berpikir) — dipakai /effort.
        `label` = label aksi dari web_options() (mis. "K3", "Effort: High").

        web_modes ikut dicari supaya /mode bisa memakai jalur yang sama persis:
        mekanismenya identik, yang beda cuma pengelompokannya di menu."""
        entry = next((a for a in tuple(self.web_actions) + tuple(self.web_modes)
                      if a[0] == label), None)
        if entry is None:
            raise BrowserError(f"opsi '{label}' tak dikenal untuk {self.label}")
        path = entry[1]
        # Tombol pembuka khusus aksi ini (bila ada) — penting untuk situs yang
        # kontrolnya tersebar di beberapa tempat.
        opener = entry[3] if len(entry) > 3 and entry[3] else self.web_model_button
        return hub().submit(
            lambda h: self._set_action_on_hub(h, label, path, opener),
            timeout=self.login_timeout + 60,
        )

    def clear_web_mode(self) -> str:
        """Matikan mode kerja situs — kembali ke chat biasa.

        Perlu ada karena mode itu MENEMPEL selama halaman tak dimuat ulang:
        sekali "Create Video" menyala, semua pesan berikutnya dianggap permintaan
        video. Tanpa jalan pulang, satu klik keliru mengunci sesi sampai
        browsernya ditutup."""
        if not self.web_mode_off_selector:
            raise BrowserError(
                f"cara mematikan mode di {self.label} belum dipetakan — "
                "mulai chat baru (/new) untuk kembali ke chat biasa")
        return hub().submit(self._clear_mode_on_hub,
                            timeout=self.login_timeout + 60)

    def _clear_mode_on_hub(self, h: Any) -> str:
        page, _ = self._acquire_ready_page(h, lambda m: None, lambda: None)
        loc = page.locator(self.web_mode_off_selector).first
        try:
            if loc.count() == 0:
                return f"{self.label} memang sedang di chat biasa"
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(
                f"keadaan mode {self.label} tak terbaca") from exc
        self._click_element(loc, timeout=8000)
        page.wait_for_timeout(600)
        return f"mode kerja {self.label} dimatikan — kembali ke chat biasa"

    def web_options(self) -> list[tuple[str, str]]:
        """Daftar (label, deskripsi) varian model/berpikir — untuk /effort."""
        return [(a[0], a[2]) for a in self.web_actions]

    def web_mode_options(self) -> list[tuple[str, str]]:
        """Daftar (label, deskripsi) MODE KERJA situs — untuk /mode.

        Kosong berarti situs ini belum dipetakan tombol modenya, BUKAN berarti
        ia tak punya. Bedanya penting saat memberi tahu pengguna."""
        return [(m[0], m[2]) for m in self.web_modes]

    # ---- hook opsional untuk subclass ----
    def _is_done(self, page: Any) -> bool:
        """Balasan sudah tuntas? Dua penanda dipakai bila situs menyediakannya:
        indikator streaming hilang DAN tombol "berhenti" tak lagi ada. Tanpa
        keduanya, jatuh ke True -> murni mengandalkan kestabilan teks."""
        try:
            if self.streaming_selector and \
                    page.query_selector(self.streaming_selector) is not None:
                return False
            for sel in self.stop_selectors:
                if page.query_selector(sel) is not None:
                    return False
        except Exception:  # noqa: BLE001
            return True
        return True

    # ---- internal (berjalan DI thread hub) ----
    def _connect_on_hub(
        self, h: Any, on_status: StatusCb | None, cancel_event: Any
    ) -> bool:
        from .. import llm  # impor tunda: hindari siklus impor

        def status(msg: str) -> None:
            if on_status:
                on_status(msg)

        def check_cancel() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise llm.Cancelled()

        status(f"menghubungkan ke {self.label}…")
        _, did_login = self._acquire_ready_page(h, status, check_cancel)
        return did_login

    def _send_on_hub(
        self,
        h: Any,
        prompt: str,
        on_status: StatusCb | None,
        on_token: TokenCb | None,
        cancel_event: Any,
        new_chat: bool = False,
        complete_when: Callable[[str], bool] | None = None,
        open_chat_id: str = "",
        attachments: list[str] | None = None,
    ) -> str:
        from .. import llm  # untuk llm.Cancelled (impor tunda: hindari siklus)

        def status(msg: str) -> None:
            if on_status:
                on_status(msg)

        def check_cancel() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise llm.Cancelled()

        status("menyiapkan sesi browser…")
        page, _ = self._acquire_ready_page(
            h, status, check_cancel, force_new_chat=new_chat,
            open_chat_id=open_chat_id)

        # --- kirim prompt ---
        check_cancel()
        # Sudah kena limit sebelum mengetik? Jangan buang waktu mengirim.
        self._raise_if_limited(page)
        self._status_captcha = status
        status(f"mengetik pesan ke {self.label}…")
        # Mencari kotak input SEKALIGUS menunggu sampai bisa diisi (situs
        # mengunci komposer selagi menjawab). Bisa dibatalkan dengan Ctrl+C.
        inp = self._input_locator(page, check_cancel)
        self._focus_input(inp)
        # Lampiran diunggah SEBELUM teks dikirim — kalau Enter ditekan lebih
        # dulu, pesan terkirim tanpa gambarnya.
        if attachments:
            status(f"mengunggah {len(attachments)} lampiran…")
            self._attach_files(page, attachments, check_cancel)
            self._focus_input(inp)
        counts_before = self._msg_counts(page)
        text_before = self._read_last_message(page)
        if self._editor_kaya(inp):
            self._ketik_contenteditable(page, inp, prompt)
        else:
            # Batas waktu eksplisit: kotak sudah dipastikan bisa diisi di atas,
            # jadi tak perlu menunggu 30 detik bawaan Playwright lagi.
            inp.fill(prompt, timeout=10000)
        self._submit(page, inp)

        # --- tunggu jawaban MULAI (streaming muncul / jumlah pesan bertambah) ---
        status(f"{self.label} sedang berpikir…")
        t0 = time.time()
        started = False
        next_limit_check = 0.0
        # Intipan captcha dimulai dari nol untuk giliran ini: yang membatasi
        # frekuensinya waktu (lihat _intip_captcha), dan sisa hitungan giliran
        # sebelumnya tak boleh menunda pemeriksaan pertama.
        self._captcha_berikut = 0.0
        while time.time() - t0 < self.start_timeout:
            check_cancel()
            # Captcha diintip BERKALA, bukan sesudah menyerah. Selama kotaknya
            # tampil, situs menahan jawabannya di belakang layar verifikasi:
            # menunggu lebih lama tak pernah menolong, dan yang dilihat pengguna
            # cuma "sedang berpikir…" berkepanjangan. Kalau ditemukan, ia
            # ditangani sampai selesai lalu penantian ini diulang dari nol.
            if self._intip_captcha(page, status, check_cancel):
                t0 = time.time()
                status(f"{self.label} sedang berpikir…")
            # Limit paling sering baru MUNCUL tepat setelah prompt dikirim —
            # laporkan segera daripada menunggu jawaban yang tak akan datang.
            # Dijeda (bukan tiap 300 ms) karena pemeriksaannya memindai DOM.
            if time.time() >= next_limit_check:
                next_limit_check = time.time() + self.limit_poll_seconds
                self._raise_if_limited(page)
            if self._answer_started(page, counts_before, text_before):
                started = True
                break
            # Sinyal "mulai" bisa TERLEWAT (balasan sangat cepat, atau situs tak
            # memberi penanda). Kalau situs sudah menyatakan selesai dan ada teks
            # yang bisa dibaca, jangan menunggu sampai batas waktu — lanjut saja.
            if time.time() - t0 > 6 and self._is_done(page) and \
                    self._read_last_message(page):
                started = True
                break
            page.wait_for_timeout(300)
        if not started and not self._read_last_message(page):
            # Captcha yang muncul SESUDAH pesan terkirim: jawabannya tertahan di
            # belakang kotak verifikasi, jadi menunggu lebih lama tak menolong.
            # Sesudah diselesaikan, situs melanjutkan sendiri permintaan yang
            # tertahan — karena itu penantiannya diulang, bukan digagalkan.
            if self.tangani_captcha(page, status, check_cancel):
                t0 = time.time()
                while time.time() - t0 < self.start_timeout:
                    check_cancel()
                    if self._answer_started(page, counts_before, text_before):
                        started = True
                        break
                    page.wait_for_timeout(300)
        if not started and not self._read_last_message(page):
            self._raise_if_limited(page)   # penyebab paling umum
            # Percakapan sudah kepanjangan: situs menolak melanjutkan sampai
            # sesi baru dibuka. Diperiksa SEBELUM spanduk galat umum supaya
            # sebabnya yang spesifik yang dilaporkan — hanya sebab inilah yang
            # punya jalan keluar otomatis (ringkas lalu pindah chat).
            self._raise_if_context_full(page)
            # Spanduk galat situs. Diperiksa SESUDAH limit karena limit lebih
            # sering, tapi SEBELUM kesimpulan "selector usang" — tuduhan itu
            # menyesatkan bila yang terjadi sebenarnya situsnya menolak.
            self._raise_if_error(page)
            raise BrowserError(
                f"balasan tak terdeteksi dari {self.label} — kemungkinan selector "
                "pesan usang untuk layout situs sekarang. Laporkan/perbarui "
                "message_selector di connectors/"
                f"{self.service}.py."
            )

        # Jawaban sudah MULAI mengalir -> ubah fase jadi "menjawab" (bukan diam
        # di "berpikir"), supaya terminal mencerminkan keadaan sebenarnya.
        status(f"{self.label} sedang menjawab…")
        _t_started = time.time()   # batas fase berpikir->menjawab (untuk ETA)

        # --- pantau teks balasan terakhir sampai stabil ---
        last = ""
        emitted = 0
        stable = 0
        deadline = time.time() + self.answer_timeout
        while time.time() < deadline:
            check_cancel()
            # Captcha bisa menyusul SESUDAH jawaban mulai mengalir: teksnya
            # membeku di tengah dan situs tak pernah menyatakan selesai. Tanpa
            # intipan di sini, gilirannya berputar sampai batas waktu penuh.
            if self._intip_captcha(page, status, check_cancel):
                deadline = time.time() + self.answer_timeout
                status(f"{self.label} sedang menjawab…")
                continue
            cur = self._read_last_message(page)
            # Yang terbaca masih BALASAN LAMA? Wadah pesan yang baru sering
            # belum berisi apa-apa untuk beberapa saat (di kimi.com bilah tombol
            # aksi ikut cocok selector dan teksnya kosong), sehingga pemindaian
            # mundur jatuh ke pesan SEBELUMNYA. Tanpa penjaga ini, seluruh
            # jawaban lama diteruskan ke on_token sebagai "jawaban" giliran ini,
            # lalu `emitted` telanjur sepanjang teks lama sehingga jawaban yang
            # asli tampil terpotong di tengah kata.
            #
            # Dulu penjaga ini DILEPAS begitu situs berhenti menandai "sedang
            # mengetik", supaya balasan yang kebetulan sama persis tak
            # menggantung sampai batas waktu. Itu BUG BERAT dan terbukti di
            # pemakaian nyata: kalau jawaban belum mulai sementara penanda
            # generating tak terdeteksi (kimi.com kerap begitu), teks balasan
            # LAMA langsung diterima sebagai jawaban giliran ini — pengguna
            # bertanya hal baru tiga kali dan menerima jawaban identik yang sama
            # persis, tanpa satu pun tanda bahwa ada yang salah.
            #
            # Penentunya kini JUMLAH ELEMEN PESAN, bukan penanda mengetik.
            # Elemen balasan baru pasti bertambah begitu situs membuatnya, jadi
            # balasan yang teksnya kebetulan sama TETAP diterima (tak menggantung)
            # sementara teks basi tak pernah lolos. _msg_counts hanya dipanggil
            # pada kasus ambigu (teks == balasan lama), jadi polling tetap murah.
            if not cur or (cur == text_before
                           and not self._ada_pesan_baru(page, counts_before)):
                # Belum ada satu huruf pun yang mengalir, dan halaman diam saja.
                # Inilah jalur tempat giliran benar-benar menggantung: situs
                # sudah membuat wadah balasan (jadi "jawaban dianggap mulai")
                # tapi isinya tertahan di belakang kotak verifikasi. Dulu tak
                # ada satu pun pemeriksaan captcha di sini, sehingga terminal
                # berputar sampai batas 5 menit lalu melapor "tak ada balasan
                # baru" — kabar captcha-nya tak pernah terpicu sama sekali.
                page.wait_for_timeout(self._poll_ms)
                continue
            if on_token and len(cur) > emitted:
                on_token(cur[emitted:])
                emitted = len(cur)
            # Penjaga anti-mengoceh: balasan membengkak tak wajar ATAU ekornya
            # cuma pola pendek yang diulang berkali-kali (model terjebak repetisi).
            # Tekan STOP situs lalu hentikan pembacaan — balasan terpotong jauh
            # lebih baik daripada terminal tersandera sampai batas waktu 5 menit.
            if len(cur) >= self._MAX_REPLY_CHARS or self._mengoceh(cur):
                self._stop_generating(page)
                break
            if cur == last:
                stable += 1
                # Selesai bila: teks berhenti berubah, situs tak lagi menandai
                # "sedang mengetik", DAN (bila diminta) balasan sudah utuh
                # menurut pemanggil — mencegah berhenti saat blok usulan tool
                # baru separuh dirender.
                if stable >= self._stable_needed and self._is_done(page):
                    if complete_when is None or complete_when(cur):
                        break
                    # Syarat pemanggil tak kunjung terpenuhi padahal situs sudah
                    # selesai & teks diam: jangan menunggu sampai batas waktu
                    # (terminal terlihat macet) — terima apa adanya.
                    if stable >= self._stable_needed * 4:
                        break
            else:
                stable = 0
                last = cur
            page.wait_for_timeout(self._poll_ms)

        # Jawaban final ditentukan DULU: rekonstruksi Markdown dari HTML
        # (list/tabel/heading/kode utuh) bila diaktifkan; kalau gagal, pakai teks
        # polos yang sudah stabil.
        # `last` kosong = loop di atas tak pernah menerima satu pun teks sebagai
        # balasan giliran INI. Jangan jatuh ke _read_last_markdown: ia membaca
        # elemen pesan terakhir apa adanya, yang dalam keadaan ini justru
        # balasan LAMA — jalur inilah yang membuat jawaban basi tetap lolos ke
        # pengguna meski penjaga di loop sudah menolaknya. Lebih baik gagal
        # terang-terangan; pemanggil bisa mengirim ulang.
        if not last:
            # KEADAAN INI PUNYA BEBERAPA SEBAB YANG SANGAT BERBEDA, dan pesan
            # lamanya cuma menuduh satu (message_selector usang). Tanpa bukti,
            # yang paling sering terjadi justru tak pernah terpikir: pesannya
            # TAK PERNAH TERKIRIM. Karena itu keadaan halaman dilaporkan apa
            # adanya — sekali lihat ketahuan mana yang terjadi.
            raise BrowserError(
                f"{self.label} tidak menghasilkan balasan baru untuk pesan ini.\n"
                + self._bukti_tanpa_balasan(page, prompt, counts_before)
            )

        final = ""
        if self.read_as_markdown:
            final = self._read_last_markdown(page) or ""
        if not final:
            final = last

        # Catat ID percakapan yang sedang dipakai (untuk fitur bersih-bersih) dan
        # isi MENTAH blok kode balasan (pemanggil memakainya untuk membaca usulan
        # tool tanpa risiko rusak oleh perenderan markdown).
        #
        # DISETEL SEBELUM pemeriksaan sibuk di bawah — yang bisa melempar. Kalau
        # ditaruh sesudahnya, giliran sibuk meninggalkan kedua atribut berisi
        # nilai giliran SEBELUMNYA, dan pemanggil yang menangani WebBusyError lalu
        # membaca last_code_blocks akan memproses blok kode jawaban lama sebagai
        # usulan tool giliran ini. last_chat_id khususnya wajib: tanpanya, ulang-
        # otomatis di core tak tahu chat mana yang sudah terlanjur dibuat.
        self.last_chat_id = self.current_chat_id(page)
        self.last_code_blocks = self._read_code_blocks(page)

        # PERCAKAPAN SUDAH KEPANJANGAN. Di sini balasannya BERHASIL didapat,
        # jadi ia tak boleh dilempar sebagai galat — pekerjaan yang barusan
        # selesai akan hilang percuma. Cukup ditandai; pemanggil menyerahkan
        # jawabannya dulu, baru meringkas & pindah chat untuk pesan berikutnya.
        # Peringatan ini memang muncul lebih dulu sebagai spanduk, beberapa
        # pesan sebelum situs benar-benar menolak melanjutkan — dan justru
        # itulah jendela yang dipakai supaya perpindahannya tak terasa.
        self.konteks_penuh = bool(self.detect_context_full(page))

        # Pemberitahuan "server sedang sibuk" muncul DI TEMPAT balasan, jadi tanpa
        # pemeriksaan ini ia diteruskan sebagai jawaban model.
        #
        # KEDUA jalur baca diperiksa, dan itu bukan kehati-hatian berlebihan:
        # TERAMATI di kimi.com, pemberitahuannya lolos ke pengguna lewat hasil
        # markdown padahal teks polosnya tak memuatnya — memeriksa `last` saja
        # tidak cukup, keduanya harus dijaga.
        #
        # Diperiksa SEBELUM web_timing.record supaya giliran gagal tak mencemari
        # statistik ETA: durasinya sangat pendek dan akan membuat janji terlalu
        # optimistis, plus panjangnya mengacaukan perhitungan throughput.
        self._raise_if_busy(last)
        self._raise_if_busy(final)

        # Rekam waktu NYATA turn ini -> dasar ETA yang jujur (lihat web_timing):
        # start_latency = durasi fase berpikir, answer_dur = durasi fase menjawab.
        try:
            from .. import web_timing
            # Panjang yang dicatat WAJIB `last` (teks polos), bukan `final`
            # (markdown): on_token mengalirkan potongan `cur` dari
            # _read_last_message, jadi penghitung kemajuan di UI bersatuan teks
            # polos. Mencatat panjang markdown — yang lebih gemuk karena **, #,
            # pagar kode, pipa tabel, dan spasi hard-break — membuat throughput
            # dan sebaran panjang beda satuan dengan kemajuan yang diukur, lalu
            # ETA bias sistematis (sisa ditaksir terlalu besar, bar terlalu
            # kosong). Swa-kalibrasi TAK bisa memperbaikinya: kuantil menggeser
            # cakupan, bukan satuan.
            web_timing.record(self.service, _t_started - t0,
                              time.time() - _t_started, len(last))
        except Exception:  # noqa: BLE001 - statistik tak boleh ganggu jawaban
            pass

        if not final:
            raise BrowserError(
                f"tidak ada jawaban terbaca dari {self.label}. Coba periksa "
                "selektor pesan, atau kirim ulang."
            )
        return final

    def _set_action_on_hub(self, h: Any, label: str, path: tuple[str, ...],
                           opener: str = "") -> str:
        """Klik aksi UI (varian model / mode). Buka tombol menunya bila ada, lalu
        klik tiap teks di `path` berurutan (dukungan menu bertingkat)."""
        page, _ = self._acquire_ready_page(h, lambda m: None, lambda: None)
        # Dialog/banner yang menutupi komposer membuat klik nyata meleset, dan
        # gejalanya menyesatkan: "opsi tak bisa diklik" padahal tombolnya ada.
        self._tutup_penghalang(page)
        # Lebar layout dipastikan DI SINI, bukan cuma saat halaman dibuka: pada
        # jendela sempit situs MENYEMBUNYIKAN sebagian tombolnya (terukur di
        # dola.com — "Create Video" lenyap di bawah 990 px), dan tombol yang
        # tersembunyi itulah yang justru mau ditekan. Tanpa ini, /mode gagal
        # kadang-kadang saja — tergantung ukuran jendela browsernya.
        self._normalize_layout(page)

        opened = self._open_menu(page, opener) if opener else False
        if opener and not opened:
            raise BrowserError(
                f"menu untuk '{label}' tak mau terbuka di {self.label} "
                f"(tombol: {opener}).")

        try:
            for i, text in enumerate(path):
                self._click_menu_text(page, text)
                # Jeda antar-tingkat agar submenu sempat muncul.
                page.wait_for_timeout(500 if i < len(path) - 1 else 250)
        except Exception as exc:
            if opened:
                try:
                    page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
            raise BrowserError(
                f"opsi '{label}' tak bisa diklik di UI {self.label} — "
                "situs mungkin berubah layout / teksnya beda."
            ) from exc
        try:  # tutup menu bila masih terbuka
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        return f"'{label}' dipilih di {self.label}"

    def _menu_items(self) -> tuple[str, ...]:
        """Kandidat selector ITEM MENU, urut dari yang paling spesifik."""
        return self._as_selectors(self.menu_item_selector)

    def _open_menu(self, page: Any, opener: str) -> bool:
        """Klik tombol pembuka lalu TUNGGU item menunya muncul.

        Jeda tetap tidak cukup: tepat setelah jawaban selesai, UI situs kadang
        masih sibuk sehingga klik pertama tak terdaftar dan menu tak terbuka —
        gejalanya 'opsi tak bisa diklik' yang muncul kadang-kadang. Karena itu
        kemunculan menu ditunggu, dan pembuka diklik ulang sekali bila perlu."""
        # Tombol pembukanya DITUNGGU dulu, tak cukup diperiksa sekali: komposer
        # dirender beberapa saat sesudah halaman dinyatakan siap, dan pemeriksaan
        # seketika menangkap keadaan "belum ada" lalu menyerah. TERUKUR di
        # kimi.com — `.current-model` tak ada pada tik pertama, tapi ada di
        # setiap lebar jendela beberapa ratus milidetik kemudian. Gejalanya:
        # "menu tak mau terbuka" yang muncul hilang-timbul.
        batas = time.time() + 6.0
        btn = None
        while time.time() < batas:
            try:
                kandidat = page.query_selector(opener)
                if kandidat is not None and kandidat.is_visible():
                    btn = kandidat
                    break
            except Exception:  # noqa: BLE001 - DOM sedang transisi
                pass
            page.wait_for_timeout(250)
        if btn is None:
            return False

        for _ in range(2):
            try:
                self._click_element(btn)   # jendela di latar -> fallback dispatch
            except Exception:  # noqa: BLE001
                return False
            deadline = time.time() + 3.0
            while time.time() < deadline:
                try:
                    if page.query_selector(", ".join(self._menu_items())) is not None:
                        return True
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(150)
        return False

    def _click_menu_text(self, page: Any, text: str) -> None:
        """Klik ITEM MENU yang memuat `text`.

        Kandidat menu_item_selector dicoba BERURUTAN, dan yang pertama
        benar-benar ada yang dipakai — bukan digabung jadi satu daftar berkoma,
        karena di daftar berkoma `.first` mengambil elemen paling awal di DOM
        alih-alih yang paling spesifik. Itu penting di situs yang menu induk dan
        submenunya sama-sama memuat teks yang dicari (mis. kimi.com: item
        "Thinking effort" ikut memuat kata "Standard" milik submenunya).

        Dibatasi ke item menu supaya tak salah mengeklik elemen lain berteks
        sama di halaman."""
        esc = text.replace('"', '\\"')
        # Dicoba beberapa kali: baris tombol komposer digambar ulang beberapa
        # saat sesudah halaman siap (terukur di dola.com — tombolnya sempat ada,
        # lalu elemennya lepas dan klik yang sedang berjalan menggantung sampai
        # kehabisan waktu). Sekali gagal belum tentu tombolnya tak ada.
        pernah_ada = False
        for _ in range(3):
            for kandidat in self._menu_items():
                semua = page.locator(f'{kandidat}:has-text("{esc}")')
                try:
                    # Yang TERLIHAT diutamakan: situs kerap menyisakan salinan
                    # tersembunyi dari baris tombolnya, dan `.first` bisa jatuh
                    # ke salinan itu — yang takkan pernah bisa diklik.
                    loc = semua.locator("visible=true").first
                    if loc.count() == 0:
                        loc = semua.first
                        if loc.count() == 0:
                            continue
                except Exception:  # noqa: BLE001 - selector tak sah -> kandidat lain
                    continue
                pernah_ada = True
                try:
                    loc.scroll_into_view_if_needed(timeout=1500)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._click_element(loc, timeout=8000)
                    return
                except Exception:  # noqa: BLE001 - elemen lepas -> coba lagi
                    break
            page.wait_for_timeout(800)
        # Dua kegagalan ini butuh penanganan yang berbeda, jadi jangan disamakan:
        # yang satu berarti teksnya salah/ tombolnya tak ada, yang satu lagi
        # berarti tombolnya ADA tapi tak bisa ditekan (tertutup, atau lepas saat
        # halaman menggambar ulang).
        if pernah_ada:
            raise BrowserError(
                f"item menu '{text}' ada tapi tak bisa ditekan — mungkin "
                "tertutup elemen lain atau halaman sedang menggambar ulang")
        raise BrowserError(
            f"item menu '{text}' tak ditemukan "
            f"({', '.join(self._menu_items())})")

    @staticmethod
    def _as_selectors(sel: str | tuple[str, ...]) -> tuple[str, ...]:
        """Normalkan atribut selector (str ATAU tuple) jadi tuple kandidat.

        Satu tempat saja, supaya aturan "kandidat dicoba BERURUTAN" tak perlu
        ditulis ulang di tiap pemakai — dan kalau aturannya berubah, tak ada
        salinan yang tertinggal."""
        if not sel:
            return ()
        return (sel,) if isinstance(sel, str) else tuple(sel)

    def _input_selectors(self) -> tuple[str, ...]:
        """Kandidat selector kotak input, urut dari yang paling spesifik."""
        return self._as_selectors(self.input_selector) or ("",)

    def _input_locator(self, page: Any, check_cancel: Callable[[], None] | None = None,
                       timeout: float = 25.0) -> Any:
        """Locator kotak input yang TERLIHAT dan BISA DIISI.

        Kandidat dicoba BERURUTAN (paling spesifik dulu) dan yang dipakai adalah
        yang pertama lolos kedua syarat. Ini penting karena:
          - daftar CSS berkoma tidak menentukan prioritas: `.first` mengambil
            elemen paling awal di DOM, yang bisa saja kotak pencarian sehingga
            prompt diketik ke tempat yang salah;
          - situs mengunci komposer selagi menjawab, jadi 'terlihat' saja belum
            cukup — harus ditunggu sampai benar-benar bisa diisi.

        Locator (bukan ElementHandle) dipakai agar kebal saat situs merender
        ulang komposer, mis. sesudah ganti model lewat /effort.
        """
        deadline = time.time() + timeout
        first_visible: Any = None
        while True:
            if check_cancel is not None:
                check_cancel()
            for sel in self._input_selectors():
                loc = page.locator(f"{sel}:visible").first
                try:
                    if loc.count() == 0:
                        continue
                except Exception:  # noqa: BLE001 - selector tak sah / DOM sibuk
                    continue
                if first_visible is None:
                    first_visible = loc
                try:
                    if loc.is_editable(timeout=1000):
                        return loc
                except Exception:  # noqa: BLE001
                    continue
            if time.time() >= deadline:
                break
            page.wait_for_timeout(300)

        if first_visible is None:
            raise BrowserError(
                f"kotak input tak ditemukan ({', '.join(self._input_selectors())}). "
                "Situs mungkin berubah layout."
            )
        raise BrowserError(
            f"kotak input {self.label} terlihat tapi terkunci selama "
            f"{timeout:.0f} detik — situs mungkin masih menjawab atau sesi "
            "perlu dimuat ulang."
        )

    def _tutup_penghalang(self, page: Any) -> int:
        """Klik tombol penutup banner/dialog yang MENGHALANGI komposer.

        Yang dimaksud: persetujuan cookie, tur fitur, promo — hal yang muncul
        sekali lalu menutupi setengah halaman. Selama ia terbuka, kotak input
        memang ADA di DOM tapi tak bisa difokuskan, dan kegagalannya muncul ke
        pengguna sebagai "kotak input tak bisa difokuskan" — pesan yang benar
        tapi menyesatkan, karena yang salah bukan kotak inputnya.

        Dipanggil hanya SESUDAH fokus gagal, bukan sebelum tiap pesan: banner
        begini muncul sekali seumur profil, jadi memeriksanya tiap kali cuma
        menambah kerja pada jalur yang paling sering dilalui.

        Return jumlah yang berhasil ditutup."""
        n = 0
        for sel in self.dismiss_selectors:
            try:
                loc = page.locator(sel)
                if loc.count() == 0:
                    continue
                el = loc.first
                if not el.is_visible():
                    continue
                self._click_element(el)
                n += 1
                page.wait_for_timeout(300)
            except Exception:  # noqa: BLE001 - gagal menutup != gagal mengirim
                continue
        return n

    def _focus_input(self, inp: Any) -> None:
        """Fokuskan kotak input dan PASTIKAN benar-benar fokus.

        `focus()` memanggil DOM focus() langsung sehingga tak perlu hit-test —
        aman saat jendela browser berjalan tersembunyi di latar, tempat klik
        mouse sungguhan selalu kehabisan waktu. Klik dipakai sebagai cadangan
        bila situs baru menyiapkan komposernya saat diklik.

        Keberhasilannya DIPERIKSA: pada kotak contenteditable, teks diketik ke
        elemen yang sedang fokus, jadi fokus yang gagal diam-diam akan mengirim
        pesan KOSONG lalu gagal dengan pesan yang menyalahkan selector pesan."""
        for percobaan in range(2):
            try:
                inp.focus(timeout=4000)
            except Exception:  # noqa: BLE001
                if percobaan == 0:
                    self._click_element(inp)   # sebagian situs baru siap saat diklik
                    continue
            if self._is_focused(inp):
                return
            if percobaan == 0:
                self._click_element(inp)
        if self._is_focused(inp):
            return
        # Belum juga fokus. Penyebab yang paling sering DAN paling mudah
        # dibereskan: ada banner yang menutupi komposer. Tutup lalu coba sekali
        # lagi sebelum menyerah — jauh lebih baik daripada menyuruh pengguna
        # membuka jendela browser untuk mengklik "OK" sendiri.
        if self.dismiss_selectors:
            try:
                page = inp.page
            except Exception:  # noqa: BLE001
                page = None
            if page is not None and self._tutup_penghalang(page):
                try:
                    inp.focus(timeout=4000)
                except Exception:  # noqa: BLE001
                    self._click_element(inp)
                if self._is_focused(inp):
                    return
        raise BrowserError(
            f"kotak input {self.label} tak bisa difokuskan — pesan tak akan "
            "sampai. Coba kirim ulang; bila terus terjadi, buka jendela "
            "browsernya (CONNECTOR_HEADLESS=false) untuk melihat keadaannya."
        )

    @staticmethod
    def _is_focused(inp: Any) -> bool:
        """True bila elemen ini yang sedang memegang fokus di halaman."""
        try:
            return bool(inp.evaluate("el => el === document.activeElement"))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _click_element(loc: Any, timeout: float | None = None) -> None:
        """Klik elemen; bila klik mouse nyata gagal, kirim event klik langsung.

        Browser connector berjalan DI LATAR dengan jendela tersembunyi, dan di
        keadaan itu klik mouse sungguhan tak bisa melakukan hit-test sehingga
        selalu kehabisan waktu (terbukti: klik biasa GAGAL, dispatch BERHASIL).
        Klik nyata tetap dicoba lebih dulu karena paling setia meniru pengguna.

        `timeout` membatasi tunggu dispatch-nya. Bawaannya 30 detik, dan itu
        terlalu lama untuk pemanggil yang mau mengulang sendiri: elemen yang
        lepas menahan satu percobaan penuh sampai batasnya habis."""
        try:
            loc.click(timeout=4000)
        except Exception:  # noqa: BLE001
            if timeout is None:
                loc.dispatch_event("click")
            else:
                loc.dispatch_event("click", timeout=timeout)

    # ---- pembacaan pesan (multi-kandidat, tahan perubahan layout) ----
    def _msg_selectors(self) -> tuple[str, ...]:
        return self._as_selectors(self.message_selector)

    def _msg_counts(self, page: Any) -> dict[str, int]:
        out: dict[str, int] = {}
        for sel in self._msg_selectors():
            try:
                out[sel] = len(page.query_selector_all(sel))
            except Exception:  # noqa: BLE001
                out[sel] = 0
        return out

    def _is_generating(self, page: Any) -> bool:
        """Situs sedang AKTIF menghasilkan jawaban? Ditandai indikator streaming
        ATAU adanya tombol "berhenti" — penanda paling andal bahwa AI sudah mulai
        bekerja, termasuk saat ia menjalankan kode/analisis dan teks jawabannya
        belum sempat muncul. Tanpa ini, fase kerja panjang salah dibaca sebagai
        "balasan tak terdeteksi" dan gagal di start_timeout padahal AI-nya
        normal, hanya sedang berpikir lama."""
        try:
            if self.streaming_selector and \
                    page.query_selector(self.streaming_selector) is not None:
                return True
            for sel in self.stop_selectors:
                if page.query_selector(sel) is not None:
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    @staticmethod
    def _mengoceh(text: str) -> bool:
        """True bila EKOR balasan cuma pola pendek yang diulang berkali-kali —
        ciri model terjebak repetisi (mis. menulis baris kode identik ribuan
        kali). Hanya diperiksa saat teks sudah cukup panjang supaya balasan sah
        yang kebetulan memuat sedikit pengulangan tak salah dipotong."""
        if len(text) < 12000:
            return False
        ekor = text[-2400:]
        # Unit ekor beberapa ukuran: pola berulang bisa 1 baris pendek atau
        # beberapa baris. Bila satu unit menyusun sebagian besar ekor (non-tumpang
        # tindih ≥30x), itu repetisi yang jelas, bukan prosa/kode wajar.
        for n in (16, 32, 64):
            unit = ekor[-n:].strip()
            if len(unit) >= 3 and ekor.count(unit) >= 30:
                return True
        return False

    def _stop_generating(self, page: Any) -> None:
        """Best-effort: tekan tombol STOP situs agar generasi berhenti. Diam bila
        tombolnya tak ada/berubah — pemanggil tetap keluar dari loop."""
        for sel in self.stop_selectors:
            try:
                loc = page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    self._click_element(loc.first)
                    return
            except Exception:  # noqa: BLE001
                pass

    def _answer_started(self, page: Any, counts_before: dict[str, int],
                        text_before: str = "") -> bool:
        """Balasan baru sudah mulai muncul?

        Tiga petunjuk dicoba — cukup salah satu. Tanpa petunjuk teks, situs yang
        MEMAKAI ULANG wadah pesan yang sama (jumlahnya tak bertambah) membuat
        bagas-ai menunggu sia-sia sampai batas waktu & terasa macet."""
        if self._is_generating(page):
            return True
        now = self._msg_counts(page)
        if any(now.get(s, 0) > n for s, n in counts_before.items()):
            return True
        cur = self._read_last_message(page)
        return bool(cur) and cur != text_before

    def _ada_pesan_baru(self, page: Any, counts_before: dict[str, int]) -> bool:
        """Situs sudah membuat elemen pesan BARU sejak prompt dikirim?

        Ini satu-satunya petunjuk yang tak ambigu ketika teks yang terbaca sama
        persis dengan balasan sebelumnya: penanda "sedang mengetik" bisa saja
        tak terdeteksi, dan teks yang identik bisa berarti dua hal yang sangat
        berbeda — balasan basi, atau jawaban baru yang kebetulan sama.
        """
        try:
            now = self._msg_counts(page)
        except Exception:  # noqa: BLE001 - DOM sibuk/berubah -> anggap belum ada
            return False
        return any(now.get(s, 0) > n for s, n in counts_before.items())

    def _is_noise(self, text: str) -> bool:
        """True bila teks HANYA chrome UI situs (mis. 'Thought for 2s'), bukan
        jawaban sesungguhnya."""
        if not self.noise_pattern:
            return False
        return bool(re.fullmatch(self.noise_pattern, (text or "").strip(),
                                 re.DOTALL))

    def _read_last_message(self, page: Any) -> str:
        """Teks pesan jawaban TERAKHIR — kandidat selector dicoba berurutan.
        Dipakai untuk deteksi kestabilan (poll), jadi sengaja teks polos & cepat.
        Teks yang cuma indikator berpikir dilewati (bukan jawaban).

        Seluruh pemindaian dikerjakan DI HALAMAN dalam SATU evaluate (lihat
        JS_LAST_TEXT): tiap putaran polling hanya sekali bolak-balik, dan tak ada
        handle elemen yang bisa jadi basi saat situs merender ulang DOM."""
        try:
            teks = page.evaluate(JS_LAST_TEXT, {
                "selectors": list(self._msg_selectors()),
                "buang": list(self.strip_selectors),
                "noise": self.noise_pattern,
            })
        except Exception:  # noqa: BLE001 - DOM sedang transisi
            return ""
        return (teks or "").strip()

    def _bukti_tanpa_balasan(self, page: Any, prompt: str,
                             counts_before: Any) -> str:
        """Keadaan halaman saat balasan tak kunjung datang — untuk dilaporkan.

        Tiga fakta yang memisahkan sebab-sebab yang selama ini tercampur:

          1. PESANNYA ADA DI HALAMAN? Kalau tidak, ia tak pernah terkirim —
             dan seluruh tuduhan soal selector jawaban salah alamat.
          2. JUMLAH ELEMEN PESAN bertambah atau tidak? Bertambah berarti situs
             MEMBUAT wadah balasan tapi isinya tak terbaca (di situlah selector
             benar-benar tersangka); tetap berarti situs memang tak menjawab.
          3. Ekor teks yang terbaca — memastikan yang terbaca memang balasan
             lama, bukan sesuatu yang lain.

        Semua pembacaan di sini dibungkus: fungsi yang dipakai untuk MELAPORKAN
        kegagalan tak boleh ikut gagal dan menutupi kegagalan aslinya."""
        baris = []
        try:
            cuplik = " ".join((prompt or "").split())[:60]
            isi = page.evaluate("() => document.body.innerText || ''") or ""
            baris.append("- pesanku terlihat di halaman: "
                         + ("YA" if cuplik and cuplik in " ".join(isi.split())
                            else "TIDAK — kemungkinan besar tak pernah terkirim"))
        except Exception:  # noqa: BLE001
            baris.append("- pesanku terlihat di halaman: (tak terbaca)")
        try:
            kini = self._msg_counts(page)
            tambah = self._ada_pesan_baru(page, counts_before)
            baris.append(f"- elemen pesan: {counts_before} -> {kini}"
                         + ("  (ada wadah balasan BARU, tapi isinya tak terbaca "
                            "-> message_selector patut dicurigai)" if tambah
                            else "  (tak ada wadah balasan baru -> situsnya "
                                 "memang tak menjawab)"))
        except Exception:  # noqa: BLE001
            baris.append("- elemen pesan: (tak terbaca)")
        try:
            ekor = " ".join((self._read_last_message(page) or "").split())[-160:]
            baris.append(f"- yang terbaca terakhir: {ekor or '(kosong)'}")
        except Exception:  # noqa: BLE001
            pass
        jejak = self._rekam_halaman(page)
        if jejak:
            baris.append(f"- rekaman halaman saat macet: {jejak}")
        baris.append("Kirim ulang pesannya. Bila terus berulang DAN barisnya "
                     "menyebut wadah balasan baru, perbarui message_selector di "
                     f"connectors/{self.service}.py.")
        return "\n".join(baris)

    def _rekam_halaman(self, page: Any) -> str:
        """Simpan tangkapan layar + HTML halaman; kembalikan lokasinya.

        Kegagalan "tak ada balasan" yang tak bisa dijelaskan selalu berakhir
        sama: keadaan halamannya sudah hilang sebelum sempat dilihat, dan
        sebabnya cuma bisa ditebak. Rekaman ini yang mengubah tebakan jadi
        bukti — terutama untuk kotak verifikasi yang teksnya tak terbaca sama
        sekali (digambar di canvas, atau bersembunyi di shadow DOM), yang dari
        terminal tak bisa dibedakan dari situs yang sekadar diam.

        Hanya beberapa rekaman terakhir yang disimpan: ini alat pelacak, bukan
        arsip, dan tangkapan layar percakapan pribadi tak boleh menumpuk."""
        try:
            folder = Path(config.CONFIG_HOME) / "jejak"
            folder.mkdir(parents=True, exist_ok=True)
            cap = time.strftime("%Y%m%d-%H%M%S")
            awalan = f"{self.service}-{cap}"
            hasil = []
            try:
                png = folder / f"{awalan}.png"
                page.screenshot(path=str(png), full_page=False, timeout=8000)
                hasil.append(png)
            except Exception:  # noqa: BLE001
                pass
            try:
                html = folder / f"{awalan}.html"
                html.write_text(page.content(), encoding="utf-8", errors="replace")
                hasil.append(html)
            except Exception:  # noqa: BLE001
                pass
            if not hasil:
                return ""
            lama = sorted(folder.glob(f"{self.service}-*"))[:-12]
            for f in lama:
                try:
                    f.unlink()
                except OSError:
                    pass
            return str(hasil[0].parent / (awalan + ".*"))
        except Exception:  # noqa: BLE001
            return ""      # perekam bukti tak boleh ikut menggagalkan laporan

    def supports_attachments(self) -> bool:
        """True bila situs ini bisa menerima lampiran file dari bagas-ai."""
        return bool(self.file_input_selector)

    def _attach_files(self, page: Any, paths: list[str],
                      check_cancel: Callable[[], None]) -> None:
        """Unggah file ke komposer & TUNGGU sampai pratinjaunya muncul.

        Menunggu itu penting: menekan Enter saat unggahan belum selesai
        mengirim pesan TANPA gambar."""
        if not self.supports_attachments():
            raise BrowserError(
                f"{self.label} belum mendukung lampiran file dari bagas-ai.")
        exist = [p for p in paths if Path(p).is_file()]
        if not exist:
            return
        before = self._attach_count(page)
        try:
            self._upload(page, exist)
        except BrowserError:
            raise      # connector sudah menjelaskan sendiri apa yang gagal
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"gagal melampirkan file: {exc}") from exc

        deadline = time.time() + self.attach_timeout
        while time.time() < deadline:
            check_cancel()
            if self._attach_count(page) >= before + len(exist):
                page.wait_for_timeout(400)   # beri jeda agar unggahan tuntas
                return
            # Situs punya BATAS jumlah berkas per percakapan (chat.z.ai: 10).
            # Begitu kena, berkas berikutnya ditolak DIAM-DIAM: pratinjaunya tak
            # pernah muncul, jadi tanpa pemeriksaan ini penantian di bawah habis
            # 90 detik lalu gagal dengan alasan yang salah ("unggahan tak
            # selesai") — padahal pesannya masih bisa dikirim, cuma tanpa
            # gambar.
            self._raise_if_attach_limit(page)
            page.wait_for_timeout(400)
        # Sebelum menyerah, sekali lagi: toast-nya bisa muncul terlambat.
        self._raise_if_attach_limit(page)
        # Unggahan yang tak pernah selesai juga meninggalkan kartu setengah
        # jadi di komposer; pesan berikutnya tak boleh membawanya.
        self._bersihkan_lampiran(page)
        raise BrowserError(
            f"unggahan lampiran ke {self.label} tak selesai dalam "
            f"{self.attach_timeout:.0f} detik.")

    def _bersihkan_lampiran(self, page: Any, batas: int = 15) -> int:
        """Lepas semua kartu lampiran dari komposer. Kembalikan jumlah yang
        berhasil dilepas (best-effort; kegagalan di sini tak fatal)."""
        if not self.attach_clear_selector:
            return 0
        lepas = 0
        for _ in range(batas):
            try:
                loc = page.locator(self.attach_clear_selector)
                if not loc.count():
                    break
                self._click_element(loc.first, timeout=4000)
                lepas += 1
                page.wait_for_timeout(350)
            except Exception:  # noqa: BLE001 - kartu keburu hilang/DOM berubah
                break
        return lepas

    def detect_attach_limit(self, page: Any) -> str:
        """Teks pemberitahuan "batas jumlah berkas" bila sedang tampil."""
        if not self.attach_limit_patterns:
            return ""
        try:
            return page.evaluate(JS_FIND_TEXT, {
                "patterns": list(self.attach_limit_patterns),
                "exclude": list(self.limit_exclude_selectors),
            }) or ""
        except Exception:  # noqa: BLE001
            return ""

    def _raise_if_attach_limit(self, page: Any) -> None:
        """Batas berkas kena -> BUANG kartu yang terlanjur menempel, lalu
        laporkan.

        Membuangnya bukan kerapian melainkan SYARAT: pemanggil menjawab galat
        ini dengan mengirim ULANG pesannya tanpa lampiran, dan kartu yang masih
        nangkring di komposer membuat kiriman itu ikut ditolak — persis
        kegagalan yang sama, sekali lagi. Situsnya pun tak akan menerima
        berkasnya: jatahnya sudah habis, jadi kartu itu tak akan pernah bisa
        berangkat."""
        pesan = self.detect_attach_limit(page)
        if not pesan:
            return
        self._bersihkan_lampiran(page)
        raise WebLampiranPenuhError(" ".join(pesan.split())[:160])

    # Taruh CARET di dalam elemen contenteditable, di akhir isinya.
    #
    # `focus()` saja TIDAK cukup: ia menjadikan elemen document.activeElement,
    # tetapi tak menaruh titik sisip. Klik sungguhan menaruhnya — namun connector
    # sering berjalan dengan jendela TERSEMBUNYI, dan di sana klik nyata gagal
    # hit-test sehingga jatuh ke dispatch_event('click'), yang merupakan event
    # SINTETIS dan sama sekali tak memindahkan caret. Range eksplisit bekerja di
    # kedua keadaan.
    _JS_TARUH_CARET = r"""
    (el) => {
      el.focus();
      try {
        const r = document.createRange();
        r.selectNodeContents(el);
        r.collapse(false);            // titik sisip di AKHIR isi
        const s = window.getSelection();
        s.removeAllRanges();
        s.addRange(r);
        return true;
      } catch (e) { return false; }
    }
    """

    def _taruh_caret(self, inp: Any) -> None:
        try:
            inp.evaluate(self._JS_TARUH_CARET)
        except Exception:  # noqa: BLE001 - dinilai lewat pemeriksaan isi kotak
            pass

    def _editor_kaya(self, inp: Any) -> bool:
        """Kotak ketik ini editor kaya (contenteditable), bukan <textarea>?

        Ditanyakan ke ELEMENNYA, bukan hanya dibaca dari atribut kelas, karena
        satu situs bisa punya DUA macam komposer sekaligus: dola.com memakai
        <textarea> untuk chat biasa, lalu menggantinya dengan editor ProseMirror
        begitu mode "Create Video" dinyalakan lewat /mode. Kalau jenisnya
        ditetapkan di kelas saja, prompt di mode itu diisi dengan cara yang salah
        dan tak pernah mendarat — gagal senyap, tanpa galat apa pun.

        Atribut kelasnya tetap dihormati sebagai penegasan: situs yang sudah
        dipastikan selalu contenteditable tak perlu ditanya tiap kali."""
        if self.input_is_contenteditable:
            return True
        try:
            return bool(inp.evaluate("e => e.isContentEditable"))
        except Exception:  # noqa: BLE001 - elemen lepas -> perlakukan sbg biasa
            return False

    def _ketik_contenteditable(self, page: Any, inp: Any, prompt: str) -> None:
        """Ketik ke editor contenteditable & PASTIKAN teksnya benar-benar masuk.

        TERBUKTI di kimi.com: tanpa caret, Input.insertText tak mendarat di mana
        pun. Gejalanya paling menyesatkan — tak ada galat sama sekali, kotak
        input tetap KOSONG, situs tak pernah menerima apa-apa, lalu giliran diam
        di "berpikir" sampai batas waktu habis.

        Karena itu hasilnya DIPERIKSA, bukan diasumsikan: caret ditaruh dulu,
        lalu isi kotak dibaca kembali. Bila masih kosong, dicoba sekali lagi
        dengan klik sungguhan lebih dulu (berguna pada situs yang baru
        menyiapkan editornya saat disentuh). Kalau tetap gagal, lebih baik
        berhenti dengan pesan jelas daripada menunggu jawaban yang tak akan
        datang."""
        for percobaan in range(2):
            if percobaan:
                self._click_element(inp)
                page.wait_for_timeout(150)
            self._taruh_caret(inp)
            page.keyboard.insert_text(prompt)
            page.wait_for_timeout(150)
            if self._input_text(inp):
                return
            # Kotak yang tak terbaca karena JENDELANYA SUDAH TAK ADA jangan
            # dilaporkan sebagai "editor menolak pengetikan" — sebabnya beda,
            # dan yang ini bisa dipulihkan sendiri dengan membuka sesi lagi.
            if not self._halaman_hidup(page):
                galat = BrowserError(
                    f"jendela/tab {self.label} tertutup saat pesan sedang "
                    "diketik, jadi pesannya tak pernah berangkat.")
                galat.target_mati = True
                raise galat
        raise BrowserError(
            f"teks tak mau masuk ke kotak input {self.label} — editornya "
            "mungkin menolak pengetikan terprogram. Coba kirim ulang; bila "
            "terus terjadi, periksa input_selector di connectors/"
            f"{self.service}.py."
        )

    # Berapa lama menunggu situs SELESAI menjawab sebelum menyatakan komposer
    # terkunci. Jawaban panjang di chat.z.ai bisa menulis semenit lebih, dan
    # menyerah lebih cepat berarti menuduh komposer rusak padahal ia cuma
    # sedang dipakai.
    _TUNGGU_SELESAI = 120.0

    # Saluran status yang dipakai _submit & penanganan captcha. Dititipkan
    # _send_on_hub tiap kirim: keduanya tak menerima on_status sebagai argumen,
    # sementara justru di situlah pengguna paling butuh diberi tahu.
    _status_captcha: Any = None
    # Saluran KABAR ke terminal (tercetak permanen, bukan baris status yang
    # berkedip). Diisi send(); lihat catatan di sana.
    _kabar: Any = None

    def _submit(self, page: Any, inp: Any) -> None:
        """Kirim pesan, dan PASTIKAN benar-benar terkirim.

        Menekan Enter saja tidak cukup di semua keadaan (lihat
        send_button_selector). Karena itu hasilnya DIPERIKSA: kotak input yang
        sudah kosong = terkirim; kalau masih berisi, tombol kirim diklik sebagai
        cadangan. Tanpa tombol kirim yang dikenal, perilakunya sama seperti
        dulu — tekan Enter lalu biarkan penantian jawaban yang menilai."""
        # Potret SEBELUM mengirim. Isi kotak input bukan satu-satunya bukti
        # (lihat _sudah_terkirim), dan pembandingnya harus diambil sekarang.
        counts0 = self._msg_counts(page)
        try:
            url0 = page.url or ""
        except Exception:  # noqa: BLE001
            url0 = ""
        if self._editor_kaya(inp):
            # Editor kaya memperbarui keadaan internalnya lewat event; menekan
            # Enter pada tik yang sama bisa mengirim keadaan yang belum sinkron.
            page.wait_for_timeout(200)
        page.keyboard.press(self.submit_key)
        if not self.send_button_selector:
            return
        # Diperiksa DULU baru menunggu: pada jalur normal (Enter memang bekerja)
        # kotaknya sudah kosong seketika, jadi tak ada jeda yang ditambahkan ke
        # tiap pengiriman. Dulu urutannya terbalik dan setiap giliran membayar
        # 500 ms percuma — pada satu sesi agent 24 langkah itu 12 detik.
        for _ in range(6):          # ~3 detik
            if self._sudah_terkirim(page, inp, counts0, url0):
                return              # kotak kosong -> pesan sudah berangkat
            page.wait_for_timeout(500)
        # Enter tak mempan. Coba tombol kirim — dan VERIFIKASI hasilnya, jangan
        # menyerah diam-diam: teks yang gagal berangkat NANGKRING di komposer
        # browser (terlihat pengguna sebagai prompt [SISTEM] yang tak pernah
        # terkirim) sementara penantian jawaban menggantung sia-sia sampai
        # timeout. Situs sesekali mengunci komposer sejenak (mis. sesudah
        # memotong output panjangnya — "Output stopped"), jadi diberi beberapa
        # putaran sebelum dinyatakan gagal dengan pesan yang jelas.
        for putaran in range(3):
            try:
                btn = page.locator(self.send_button_selector).first
                if btn.count():
                    self._click_element(btn)
            except Exception:  # noqa: BLE001 - dinilai lewat isi kotak di bawah
                pass
            for _ in range(6):      # ~3 detik per putaran
                if self._sudah_terkirim(page, inp, counts0, url0):
                    return
                page.wait_for_timeout(500)
            page.keyboard.press(self.submit_key)   # coba Enter sekali lagi
            page.wait_for_timeout(400)
            if self._sudah_terkirim(page, inp, counts0, url0):
                return
        # SEBELUM APA PUN: captcha? Itu penyebab yang paling sering menyamar
        # jadi "komposer menolak Enter" — dan satu-satunya yang butuh tangan
        # manusia. Ditangani lebih dulu supaya pengguna tak dikirimi tuduhan
        # yang salah lalu disuruh mengirim ulang ke kotak yang memang terkunci.
        if self.tangani_captcha(page, self._status_captcha, None):
            page.keyboard.press(self.submit_key)
            page.wait_for_timeout(600)
            if self._sudah_terkirim(page, inp, counts0, url0):
                return
            try:
                btn = page.locator(self.send_button_selector).first
                if btn.count():
                    self._click_element(btn)
            except Exception:  # noqa: BLE001
                pass
            for _ in range(8):
                if self._sudah_terkirim(page, inp, counts0, url0):
                    return
                page.wait_for_timeout(500)

        # SEBELUM MENYERAH: situsnya mungkin MASIH MENJAWAB pesan sebelumnya.
        # Komposer memang dikunci selama itu, dan tak ada spanduk apa pun yang
        # muncul — jadi gejalanya persis "komposer menolak Enter", tuduhan yang
        # salah sasaran dan tak bisa ditindaklanjuti pengguna.
        #
        # Terjadi berpasangan dengan kegagalan sebelumnya: giliran yang berakhir
        # "tak ada balasan baru" bisa meninggalkan situs yang masih menulis, dan
        # kiriman berikutnya menabrak kunci itu. Karena itu ditunggu — bukan
        # ditebak — lalu dicoba sekali lagi.
        if not self._is_done(page):
            habis = time.time() + self._TUNGGU_SELESAI
            while time.time() < habis and not self._is_done(page):
                if self._sudah_terkirim(page, inp, counts0, url0):
                    return          # ternyata berangkat juga
                # "Situs masih menulis" dan "captcha mengunci komposer" tampak
                # sama persis dari luar. Menunggu 2 menit untuk yang kedua cuma
                # membuang waktu pengguna: yang dibutuhkan tangannya.
                self._intip_captcha(page, self._status_captcha, None)
                page.wait_for_timeout(500)
            page.keyboard.press(self.submit_key)
            page.wait_for_timeout(400)
            if self._sudah_terkirim(page, inp, counts0, url0):
                return
            try:
                btn = page.locator(self.send_button_selector).first
                if btn.count():
                    self._click_element(btn)
            except Exception:  # noqa: BLE001
                pass
            for _ in range(6):
                if self._sudah_terkirim(page, inp, counts0, url0):
                    return
                page.wait_for_timeout(500)

        # Menyerah. Teks yang gagal berangkat WAJIB disapu dulu: percobaan
        # berikutnya mengetik dengan insert_text di posisi caret, jadi sisa
        # pesan lama membuat kiriman ulang berisi pesan yang sama DUA KALI
        # sambung-menyambung — model lalu membacanya sebagai satu permintaan
        # aneh yang panjang.
        self._kosongkan_kotak(page, inp)
        # Kartu lampiran ikut disapu: kalau pengirimannya gagal justru KARENA
        # lampiran (mis. jatah berkas habis), meninggalkannya di komposer
        # membuat percobaan berikutnya gagal dengan alasan yang sama.
        self._bersihkan_lampiran(page)
        # Baru sesudah itu penyebabnya ditanyakan ke halaman. TERLIHAT di
        # kimi.com: spanduk "Server exception, please try again later." muncul
        # dan komposernya ikut terkunci — gejalanya di terminal justru
        # "komposer menolak Enter", tuduhan yang salah sasaran dan tak bisa
        # ditindaklanjuti pengguna. Yang benar: itu galat SEMENTARA milik situs,
        # jadi diangkat sebagai WebBusyError supaya core menunggu lalu mengirim
        # ulang sendiri (15/40/75 detik) tanpa menggagalkan giliran.
        self._raise_if_busy_page(page)
        # Komposer juga bisa terkunci karena percakapannya memang sudah penuh
        # atau kuota habis; keduanya punya jalan keluar sendiri (pemadatan
        # otomatis / tanya pengguna) yang jauh lebih berguna daripada menyuruh
        # "coba kirim ulang" ke kotak yang tak akan menerima apa pun.
        self._raise_if_context_full(page)
        self._raise_if_limited(page)
        # Keadaan situs ikut disebut: "masih menulis" dan "diam tapi menolak"
        # adalah dua masalah yang berbeda, dan menyamakan kalimatnya membuat
        # yang pertama tampak seperti kerusakan padahal ia cuma butuh waktu.
        try:
            masih = not self._is_done(page)
        except Exception:  # noqa: BLE001
            masih = False
        raise BrowserError(
            f"pesan tak mau terkirim di {self.label} — komposer menolak Enter "
            "maupun tombol kirim. "
            + (f"Situsnya MASIH menulis jawaban sebelumnya walau sudah ditunggu "
               f"{self._TUNGGU_SELESAI:.0f} detik — jawaban yang kepanjangan "
               "atau situs yang tersendat. "
               if masih else
               "Situsnya tak sedang menulis apa pun, jadi komposernya memang "
               "terkunci (atau selektor tombol kirimnya berubah). ")
            + "Kotak input sudah dikosongkan lagi; coba kirim ulang.")

    def _sudah_terkirim(self, page: Any, inp: Any, counts0: dict[str, int],
                        url0: str) -> bool:
        """Pesannya sudah berangkat? Tiga bukti, cukup salah satu.

        Dulu hanya SATU yang dipakai — "kotak input sudah kosong" — dan itu
        TERBUKTI keliru pada situs yang berpindah halaman saat pesan pertama
        dikirim: chat.z.ai memindahkan URL dari / ke /c/<id>, DOM lama dibuang,
        dan pembacaan isi kotak gagal di tengah navigasi. `_input_text` menjawab
        None ("tak terbaca"), yang sengaja BUKAN "kosong" — sehingga pengiriman
        yang sebenarnya BERHASIL dilaporkan gagal: pengguna melihat "komposer
        menolak Enter maupun tombol kirim" padahal pesannya sudah nangkring di
        percakapan dan modelnya sedang menjawab. Lebih buruk lagi, jalur
        cadangan lalu menekan Enter & tombol kirim berkali-kali di halaman baru.

        Dua bukti tambahan menutup lubang itu:
          * jumlah elemen pesan BERTAMBAH — gelembung pesan kita sendiri;
          * URL berubah menjadi URL percakapan (/c/<id>, /chat/<id>), yang di
            situs-situs ini hanya terjadi karena pesan pertama terkirim.
        """
        if self._kotak_kosong(page, inp):    # bukti terkuat & termurah
            return True
        try:
            sekarang = self._msg_counts(page)
            if any(sekarang.get(s, 0) > n for s, n in counts0.items()):
                return True
            url = page.url or ""
            if url != url0 and self.chat_id_pattern \
                    and re.search(self.chat_id_pattern, url):
                return True
        except Exception:  # noqa: BLE001 - DOM sedang transisi -> belum tentu
            pass
        return False

    def _kosongkan_kotak(self, page: Any, inp: Any) -> None:
        """Sapu isi komposer, sebisanya (kegagalan di sini tak fatal).

        Dipakai sesudah pengiriman gagal. <textarea> bisa dikosongkan langsung,
        editor kaya (contenteditable) tidak — di sana nilainya dikelola
        ProseMirror & kawan-kawan, jadi caranya lewat papan ketik: pilih semua
        lalu hapus, persis seperti yang dilakukan manusia."""
        try:
            self._taruh_caret(inp)
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")
            page.wait_for_timeout(120)
            if not self._input_text(inp):
                return
        except Exception:  # noqa: BLE001 - dicoba sekali lagi lewat DOM
            pass
        try:
            inp.evaluate(
                "el => { if (el.value !== undefined) { el.value = ''; } "
                "else { el.innerHTML = ''; } "
                "el.dispatchEvent(new Event('input', {bubbles: true})); }",
                timeout=2000)
        except Exception:  # noqa: BLE001 - sudah usaha terbaik
            pass

    @staticmethod
    def _input_text(inp: Any, timeout: float = 2500) -> str | None:
        """Isi kotak input saat ini, atau None bila TAK BISA DIBACA.

        Perbedaan None vs "" itu inti perbaikan sebuah bug, bukan kerapian:
        dulu keduanya dilaporkan sebagai "" — sehingga kotak yang tak bisa
        dibaca (tab tertutup, halaman berpindah, selektor tak lagi cocok) tak
        bisa dibedakan dari kotak yang memang sudah kosong. Bagi _submit,
        kotak kosong berarti "pesan sudah berangkat", jadi jendela yang
        tertutup tepat saat mengirim justru DILAPORKAN SEBAGAI SUKSES: giliran
        lalu menunggu jawaban yang tak akan pernah datang, dan pengguna
        melihat 'pesan tidak terkirim' tanpa sebab yang terlihat.

        `timeout` juga sengaja pendek. Playwright menunggu 30 detik sebelum
        menyerah pada selektor yang tak cocok, dan _submit membaca kotak ini
        berkali-kali — cukup untuk membuat satu pengiriman gagal menggantung
        bermenit-menit."""
        try:
            return (inp.evaluate(
                "el => (el.value !== undefined ? el.value : el.innerText) || ''",
                timeout=timeout,
            ) or "").strip()
        except Exception:  # noqa: BLE001 - tak terbaca != kosong
            return None

    @staticmethod
    def _halaman_hidup(page: Any) -> bool:
        """Halaman/tab-nya masih ada?"""
        try:
            if page.is_closed():
                return False
            _ = page.url
            return True
        except Exception:  # noqa: BLE001
            return False

    def _kotak_kosong(self, page: Any, inp: Any) -> bool:
        """True HANYA bila kotak input terbaca DAN isinya memang kosong.

        Kotak yang tak terbaca bukan bukti apa pun. Kalau halamannya ternyata
        sudah tak ada, itu bukan 'terkirim' melainkan jendela yang tertutup —
        dan itu dikatakan apa adanya supaya bisa dipulihkan (send() membuka
        ulang sesi lalu mengirim sekali lagi), bukan diam-diam dianggap
        berhasil."""
        isi = self._input_text(inp)
        if isi is not None:
            return not isi
        if not self._halaman_hidup(page):
            galat = BrowserError(
                f"jendela/tab {self.label} tertutup saat pesan sedang dikirim, "
                "jadi pesannya tak pernah berangkat.")
            # Ditandai supaya _is_dead_target mengenalinya dan send() memulihkan
            # sendiri — tanpa penanda ini, pesannya cuma cocok lewat teks bahasa
            # Inggris milik Playwright.
            galat.target_mati = True
            raise galat
        return False

    def _upload(self, page: Any, paths: list[str]) -> None:
        """Serahkan file ke situs. Default: isi langsung input file-nya.

        Situs yang input file-nya BARU DIBUAT saat menu lampiran dibuka
        meng-override ini (lihat KimiConnector) — di situs seperti itu
        set_input_files pada input yang sudah ada tak berpengaruh apa pun."""
        page.set_input_files(self.file_input_selector, paths)

    def _attach_count(self, page: Any) -> int:
        """Jumlah kartu/pratinjau lampiran yang sudah menempel di komposer."""
        try:
            return int(page.evaluate(JS_ATTACH_COUNT, {
                "card": self.attach_item_selector,
                "input": self._input_selectors()[0],
            }) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def detect_limit(self, page: Any) -> str:
        """Teks pemberitahuan LIMIT bila sedang tampil di halaman, else "".

        Area percakapan DIKECUALIKAN supaya jawaban AI yang membahas rate limit
        tidak dikira kuota habis."""
        if not self.limit_patterns:
            return ""
        try:
            return page.evaluate(JS_FIND_TEXT, {
                "patterns": list(self.limit_patterns),
                "exclude": list(self.limit_exclude_selectors),
            }) or ""
        except Exception:  # noqa: BLE001
            return ""

    def _raise_if_limited(self, page: Any) -> None:
        msg = self.detect_limit(page)
        if msg:
            raise WebLimitError(msg)

    def detect_captcha(self, page: Any) -> str:
        """Teks verifikasi keamanan bila sedang tampil, else "".

        Area percakapan DIKECUALIKAN dengan alasan yang sama seperti limit:
        bagas-ai dipakai untuk ngoding, dan jawaban model yang kebetulan
        membahas captcha tak boleh dikira captcha sungguhan.

        IFRAME ikut dipindai. Kotak verifikasi lazim dirender di dalam bingkai
        milik penyedia captcha, dan document.body halaman induk tak memuat
        teksnya sama sekali — pemindaian yang berhenti di dokumen utama akan
        selamanya menjawab "tak ada captcha" sementara layarnya jelas-jelas
        memintanya, dan giliran itu menggantung tanpa satu pun kabar."""
        # Penanda STRUKTURAL lebih dulu: kalau elemen kotaknya sudah dikenal,
        # tak perlu menebak-nebak lewat kalimat.
        for sel in self.captcha_selectors:
            try:
                el = page.locator(sel).first
                if not el.count() or not el.is_visible(timeout=1000):
                    continue
                kotak = el.bounding_box()
                if not kotak or kotak["width"] < 80 or kotak["height"] < 60:
                    continue        # sisa elemen yang menciut, bukan kotak nyata
                teks = el.inner_text(timeout=1000) or ""
            except Exception:  # noqa: BLE001
                continue
            # Buang glyph ikon dari font sendiri (Private Use Area): di kotak
            # Aliyun ada tiga di antara kalimatnya, dan di terminal itu tampil
            # sebagai kotak-kotak rusak di tengah pemberitahuan.
            teks = " ".join("".join(
                ch for ch in teks if not 0xE000 <= ord(ch) <= 0xF8FF).split())
            return teks[:180] or "kotak verifikasi keamanan sedang tampil"
        if not self.captcha_patterns:
            return ""
        arg = {"patterns": list(self.captcha_patterns),
               "exclude": list(self.limit_exclude_selectors)}
        try:
            hasil = page.evaluate(JS_FIND_TEXT, arg) or ""
        except Exception:  # noqa: BLE001
            hasil = ""
        if hasil:
            return hasil
        try:
            bingkai = list(page.frames)
        except Exception:  # noqa: BLE001
            return ""
        for f in bingkai:
            try:
                if f is page.main_frame:
                    continue        # sudah dipindai di atas
                teks = f.evaluate(JS_FIND_TEXT, arg) or ""
            except Exception:  # noqa: BLE001
                continue            # bingkai yang keburu ditutup / tak terbaca
            if teks:
                return teks
        return ""

    # Berapa lama menunggu captcha diselesaikan sebelum menyerah, dan berapa
    # detik pemberitahuannya tampil sebelum jendelanya dibuka. Jeda itu bukan
    # basa-basi: jendela yang tiba-tiba melompat ke depan tanpa penjelasan
    # tampak seperti program yang lepas kendali.
    _CAPTCHA_TUNGGU = 300.0
    _CAPTCHA_JEDA = 4.0
    # Sesering apa halaman diperiksa SELAGI menunggu jawaban. Dulu captcha baru
    # diperiksa sesudah seluruh penantian habis (90 detik untuk mulai, 300 detik
    # untuk selesai) — dan itu sama saja tak diperiksa: yang terlihat pengguna
    # cuma "sedang berpikir…" yang tak berujung, tanpa kabar, tanpa jendela yang
    # terbuka. Captcha muncul mendadak di tengah giliran, jadi harus DIINTIP
    # berkala, bukan ditunggu sampai menyerah.
    _CAPTCHA_PERIKSA = 5.0

    def _intip_captcha(self, page: Any, status: Any = None,
                       check_cancel: Any = None) -> bool:
        """Intip captcha, paling sering sekali per _CAPTCHA_PERIKSA detik.

        Dipanggil dari SETIAP perulangan yang bisa menggantung — menunggu
        komposer, menunggu jawaban mulai, memantau jawaban, menunggu situs
        selesai menulis. Itu bukan berlebihan: captcha muncul beberapa detik
        SESUDAH pesan terkirim (terukur di pemakaian pengguna), jadi tak ada
        satu titik pun yang bisa dipercaya sebagai "tempat memeriksanya".

        Dulu pemeriksaan di perulangan pemantau dipagari syarat "belum ada satu
        huruf pun yang mengalir". Begitu situs sempat menulis sepotong lalu
        membeku di belakang kotak verifikasi, syarat itu tak pernah terpenuhi
        lagi dan gilirannya berputar sampai batas 5 menit tanpa satu pun kabar.
        Pembatasnya kini WAKTU, bukan keadaan: sekali pindai 45 ms sekali per 5
        detik — tak ada alasan menghematnya dengan syarat yang bisa salah."""
        kini = time.time()
        if kini < getattr(self, "_captcha_berikut", 0.0):
            return False
        self._captcha_berikut = kini + self._CAPTCHA_PERIKSA
        return self.tangani_captcha(page, status, check_cancel)

    def tangani_captcha(self, page: Any, status: Any = None,
                        check_cancel: Any = None) -> bool:
        """Captcha muncul -> beri tahu, BUKA jendelanya, tunggu diselesaikan.

        Return True bila tadi memang ada captcha DAN sekarang sudah hilang.
        Sesudahnya jendela dikembalikan ke latar seperti semula, jadi pengguna
        cukup menyelesaikan tekaannya lalu kembali ke terminal."""
        pesan = self.detect_captcha(page)
        if not pesan:
            return False

        def lapor(m: str) -> None:
            """Sampaikan ke terminal lewat DUA saluran sekaligus.

            Kabar dicetak permanen ke riwayat terminal — itu yang benar-benar
            dibaca pengguna. Status cuma menghidupkan baris fase di footer, dan
            kalimat panjang di situ diringkas jadi satu kata; mengandalkannya
            saja berarti pemberitahuannya tak pernah sampai."""
            for saluran in (self._kabar, status):
                if not saluran:
                    continue
                try:
                    saluran(m)
                except Exception:  # noqa: BLE001
                    pass

        singkat = " ".join(pesan.split())[:70]
        lapor(f"⚠ {self.label} meminta VERIFIKASI KEAMANAN (captcha): "
              f"\"{singkat}\". Captcha hanya bisa diselesaikan MANUSIA, jadi "
              f"jendela browsernya akan kubuka {self._CAPTCHA_JEDA:.0f} detik "
              "lagi — selesaikan tekaannya di sana, lalu tak usah apa-apakan "
              "lagi: jendelanya kututup sendiri dan percakapan ini kulanjutkan.")
        # Diberi jeda supaya pemberitahuannya sempat terbaca SEBELUM jendelanya
        # melompat ke depan dan merebut perhatian.
        habis = time.time() + self._CAPTCHA_JEDA
        while time.time() < habis:
            if check_cancel:
                check_cancel()
            page.wait_for_timeout(200)

        self._foreground(page)
        lapor(f"jendela {self.label} sudah terbuka — selesaikan captcha-nya "
              "sekarang. Aku menunggu di sini (maksimal "
              f"{self._CAPTCHA_TUNGGU / 60:.0f} menit; Ctrl+C untuk batal).")
        batas = time.time() + self._CAPTCHA_TUNGGU
        while time.time() < batas:
            if check_cancel:
                check_cancel()
            page.wait_for_timeout(1000)
            if not self.detect_captcha(page):
                # Beri jeda sebentar: sebagian situs menutup kotaknya lebih dulu
                # baru melanjutkan permintaan yang tertahan.
                page.wait_for_timeout(1200)
                self._background(page)
                lapor(f"✓ verifikasi selesai — jendela {self.label} kembali "
                      "ke latar, percakapan dilanjutkan")
                return True
        self._background(page)
        raise BrowserError(
            f"{self.label} meminta verifikasi keamanan (captcha) dan belum "
            f"diselesaikan dalam {self._CAPTCHA_TUNGGU / 60:.0f} menit. "
            "Jendelanya sudah dibuka tadi — selesaikan tekaannya lalu kirim "
            "ulang pesannya.")

    def detect_context_full(self, page: Any) -> str:
        """Teks "percakapan sudah kepanjangan" bila sedang tampil, else "".

        Area percakapan DIKECUALIKAN dengan alasan yang sama seperti limit:
        jawaban model sendiri bisa membahas "konteks terlalu panjang", dan itu
        bukan pemberitahuan situs."""
        if not self.context_full_patterns:
            return ""
        try:
            return page.evaluate(JS_FIND_TEXT, {
                "patterns": list(self.context_full_patterns),
                "exclude": list(self.limit_exclude_selectors),
            }) or ""
        except Exception:  # noqa: BLE001
            return ""

    def _raise_if_context_full(self, page: Any) -> None:
        pesan = self.detect_context_full(page)
        if pesan:
            raise WebKonteksPenuhError(" ".join(pesan.split())[:240])

    def detect_error(self, page: Any) -> str:
        """Teks SPANDUK GALAT situs bila sedang tampil, else "".

        Dipisah dari detect_limit karena maksudnya berbeda: yang ini menangkap
        pemberitahuan bahwa permintaannya GAGAL (koneksi, chat rusak), bukan
        bahwa kuota habis."""
        if not self.error_patterns:
            return ""
        try:
            return page.evaluate(JS_FIND_TEXT, {
                "patterns": list(self.error_patterns),
                "exclude": list(self.limit_exclude_selectors),
            }) or ""
        except Exception:  # noqa: BLE001
            return ""

    def _raise_if_error(self, page: Any) -> None:
        """Ubah spanduk galat situs jadi kegagalan yang BERNAMA.

        Dipanggil hanya di jalur "balasan tak pernah muncul". Di situ giliran
        memang sudah gagal apa pun yang terjadi, jadi salah tangkap tak
        merugikan apa-apa — sementara tangkapan yang benar mengubah pesan
        "balasan tak terdeteksi, selector mungkin usang" (menuduh hal yang
        salah, dan tak bisa ditindaklanjuti) menjadi sebab yang sebenarnya."""
        pesan = self.detect_error(page)
        if not pesan:
            return
        bersih = " ".join(pesan.split())[:240]
        # Chat yang rusak bisa dipulihkan sendiri; sisanya tidak. Membedakannya
        # di sini menentukan apakah pengguna perlu diganggu atau tidak.
        if re.search(r"parent[_ ]?id|is not exist|conversation not found|"
                     r"chat not found", bersih, re.I):
            raise WebChatRusakError(bersih)
        raise BrowserError(f"{self.label} menolak permintaan: {bersih}")

    def detect_busy(self, page: Any) -> str:
        """Teks spanduk "situs sedang bermasalah/sibuk" bila sedang tampil.

        Saudara halaman dari _raise_if_busy (yang menilai TEKS BALASAN). Yang
        ini dipakai saat balasan bahkan belum sempat ada — mis. komposer
        terkunci sesudah situs memunculkan galat sementaranya. Area percakapan
        dikecualikan supaya jawaban model yang kebetulan membahas "server
        exception" tak dikira spanduk situs."""
        if not self.busy_patterns:
            return ""
        try:
            return page.evaluate(JS_FIND_TEXT, {
                "patterns": list(self.busy_patterns),
                "exclude": list(self.limit_exclude_selectors),
            }) or ""
        except Exception:  # noqa: BLE001
            return ""

    def _raise_if_busy_page(self, page: Any) -> None:
        pesan = self.detect_busy(page)
        if pesan:
            raise WebBusyError(" ".join(pesan.split())[:160])

    def _raise_if_busy(self, text: str) -> None:
        """Balasan ternyata cuma pemberitahuan "server sibuk"? -> WebBusyError,
        supaya pemanggil menunggu lalu mengulang, BUKAN menampilkannya sebagai
        jawaban model.

        Dua penjaga agar jawaban asli tak pernah salah tangkap: teks harus
        PENDEK (lihat busy_max_chars) dan harus cocok pola yang khas milik
        pemberitahuan situs."""
        if not self.busy_patterns or not text:
            return
        bersih = text.strip()
        if len(bersih) > self.busy_max_chars:
            return
        for pola in self.busy_patterns:
            if re.search(pola, bersih, re.I):
                raise WebBusyError(bersih.replace("\n", " ")[:160])

    def _read_code_blocks(self, page: Any) -> list[str]:
        """Isi MENTAH semua blok kode pada balasan terakhir (apa adanya)."""
        try:
            out = page.evaluate(JS_CODE_BLOCKS, {
                "selectors": list(self._msg_selectors()),
                "buang": list(self.strip_selectors),
            })
            return [str(x) for x in (out or [])]
        except Exception:  # noqa: BLE001
            return []

    def _read_last_markdown(self, page: Any) -> str:
        """Jawaban TERAKHIR sebagai Markdown (list/tabel/heading/kode utuh),
        direkonstruksi dari HTML yang dirender situs. Blok berpikir (mode
        reasoning) dibuang di dalam JS_TO_MARKDOWN."""
        try:
            md = page.evaluate(JS_TO_MARKDOWN, {
                "selectors": list(self._msg_selectors()),
                "buang": list(self.strip_selectors),
            })
            return (md or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    # ---- kesiapan halaman & login ----
    # Ciri galat "browser/tab-nya sudah mati" dari Playwright. Dicocokkan lewat
    # TEKS karena Playwright melaporkan semuanya sebagai Error generik — tak ada
    # tipe khusus yang bisa ditangkap.
    _DEAD_TARGET = ("has been closed", "target closed", "target crashed",
                    "browser has been closed", "connection closed")

    def _is_dead_target(self, exc: BaseException) -> bool:
        # Penanda eksplisit lebih dulu: galat yang KITA lempar sendiri tahu
        # persis bahwa targetnya mati, dan tak perlu ditebak lewat teks bahasa
        # Inggris milik Playwright (lihat _kotak_kosong).
        if getattr(exc, "target_mati", False):
            return True
        text = str(exc).lower()
        return any(m in text for m in self._DEAD_TARGET)

    def _acquire_ready_page(
        self, h: Any, status: StatusCb, check_cancel: Callable[[], None],
        force_new_chat: bool = False, open_chat_id: str = "",
    ) -> tuple[Any, bool]:
        """Seperti _acquire_once, tapi PULIH sekali bila browsernya ternyata mati.

        Context bisa mati kapan saja di antara dua pemeriksaan (Chrome ditutup,
        profilnya direbut instance lain, tab-nya hilang). Tanpa percobaan ulang,
        giliran yang kebetulan datang tepat sesudah itu gagal dengan galat
        Playwright mentah — padahal cukup dibuang lalu diluncurkan ulang.

        Sengaja menangkap Exception, bukan BrowserError saja: kematian target
        justru paling sering muncul sebagai galat Playwright MENTAH dari
        h.page_for/_launch (profil direbut Chrome sisa), yang bukan BrowserError
        — kalau hanya BrowserError yang ditangkap, pemulihan ini tak pernah
        jalan untuk kasus yang paling ia tuju. Penyaringnya tetap ketat lewat
        _is_dead_target, jadi galat lain (termasuk pembatalan) tetap dilempar."""
        try:
            return self._acquire_once(
                h, status, check_cancel, force_new_chat, open_chat_id)
        except Exception as exc:  # noqa: BLE001 - disaring _is_dead_target
            if not self._is_dead_target(exc):
                raise
        status("sesi browser mati, meluncurkan ulang…")
        h.drop(self.service)
        return self._acquire_once(
            h, status, check_cancel, force_new_chat, open_chat_id)

    def _acquire_once(
        self, h: Any, status: StatusCb, check_cancel: Callable[[], None],
        force_new_chat: bool = False, open_chat_id: str = "",
    ) -> tuple[Any, bool]:
        """Kembalikan (page siap-pakai yang SUDAH login, apakah login BARU terjadi).

        Konsep connector: browser MUNCUL sekali untuk LOGIN, lalu MINGGIR
        (di-minimize) — seluruh proses & jawaban tampil di TERMINAL, pengguna tak
        menyentuh browser lagi. Kenapa bukan headless: situs chat AI umumnya
        pakai Cloudflare, dan clearance-nya terikat fingerprint browser TAMPIL —
        di headless ditolak. Jadi jendela tetap ada tapi disembunyikan (minimize).

        CONNECTOR_HEADLESS=true = paksa headless sejati (tak tampil sama sekali)
        untuk situs yang memang lolos tanpa Cloudflare (mis. sebagian akun Qwen).
        """
        # Opt-in: headless sejati (mungkin diblok anti-bot di sebagian situs).
        if config.CONNECTOR_HEADLESS:
            page = h.page_for(self.service, headless=True)
            # Di headless jendelanya tak pernah dimaksimalkan (bawaan 800x600),
            # jadi justru di sinilah layout sempit paling mungkin terjadi.
            self._normalize_layout(page)
            if self._chat_ready(page, 1500, check_cancel):
                return page, False
            self._goto(page)
            if not self._chat_ready(page, self._NAV_READY_MS, check_cancel):
                raise BrowserError(
                    "mode headless belum siap (kemungkinan diblok anti-bot / "
                    "belum login). Hapus CONNECTOR_HEADLESS agar login via jendela."
                )
            return page, False

        # Default: jendela TAMPIL (lolos Cloudflare) lalu di-minimize.
        #
        # page_for bisa memakan BELASAN DETIK bila Chrome harus diluncurkan
        # (apalagi kalau profilnya masih dikunci proses sisa, yang berarti
        # membunuhnya dulu). Tanpa kabar apa pun di sini, terminal terlihat diam
        # tanpa sebab dan tanpa jendela yang muncul — persis kesan "programnya
        # tak melakukan apa-apa".
        status("menyiapkan jendela Chrome…")
        page = h.page_for(self.service, headless=False)
        status("menunggu halaman siap…")
        # Sudah di percakapan aktif & login? Lanjutkan (jangan buka chat baru) —
        # KECUALI diminta MEMBUKA chat lama tertentu (lanjut sesi) atau memulai
        # percakapan BARU, supaya AI web tak terbawa konteks chat yang salah.
        target = self.chat_url_for(open_chat_id)
        if self._chat_ready(page, 1500, check_cancel):
            if target and not self._on_chat(page, open_chat_id):
                self._goto(page, target)  # lanjutkan percakapan lama
                self._chat_ready(page, self._NAV_READY_MS, check_cancel)
            elif force_new_chat and not target:
                # Tombol "chat baru" milik situs lebih murah daripada memuat
                # ulang seluruh SPA. Kalau tombolnya tak ada/berubah, jatuh ke
                # navigasi biasa yang selalu bekerja.
                status("membuka percakapan baru…")
                if not (self.new_chat_selector
                        and self._click_new_chat(page, check_cancel)):
                    self._goto(page)      # buka chat baru (chat_url)
                    self._chat_ready(page, self._NAV_READY_MS, check_cancel)
            self._normalize_layout(page)
            self._background(page)
            return page, False

        self._goto(page, target or None)
        did_login = False
        if not self._chat_ready(page, self._NAV_READY_MS, check_cancel):
            # BELUM login -> jendela harus TERLIHAT supaya pengguna bisa sign-in.
            self._foreground(page)
            status(
                "🔐 Silakan SIGN-IN di jendela Chrome yang terbuka "
                "(email/Google + kode/CAPTCHA). Aku tunggu sampai selesai…"
            )
            self._wait_login(page, check_cancel)
            status("login berhasil ✓ — browser lanjut di latar, kerja di terminal")
            did_login = True
        # Sesudah login, BUKAN sebelumnya: selama pengguna sign-in, jendelanya
        # tampil dan biarlah ia memakai ukuran aslinya yang natural.
        self._normalize_layout(page)
        self._background(page)
        return page, did_login

    def _click_new_chat(self, page: Any,
                        check_cancel: Callable[[], None] | None = None) -> bool:
        """Mulai percakapan baru lewat tombol situs. True bila berhasil & siap.

        Kandidat dicoba BERURUTAN (paling spesifik dulu), dan yang pertama
        benar-benar ada yang diklik — bukan yang kebetulan paling awal di DOM.

        Visibilitas diperiksa PER ELEMEN, bukan dengan menempelkan `:visible` ke
        selectornya: bila sebuah kandidat kebetulan ditulis berkoma, akhiran itu
        hanya mengenai alternatif TERAKHIR, sehingga elemen tersembunyi dari
        alternatif pertama tetap lolos lalu diklik sia-sia.

        Sengaja mengembalikan bool alih-alih melempar: ini OPTIMASI, bukan
        keharusan — kalau tombolnya berubah/hilang, pemanggil cukup jatuh ke
        navigasi biasa."""
        for kandidat in self._as_selectors(self.new_chat_selector):
            try:
                loc = page.locator(kandidat)
                for i in range(min(loc.count(), 5)):
                    tombol = loc.nth(i)
                    if not tombol.is_visible():
                        continue
                    self._click_element(tombol)
                    page.wait_for_timeout(800)
                    if self._chat_ready(page, 8000, check_cancel):
                        return True
                    break        # tombolnya benar tapi belum siap -> navigasi biasa
            except Exception:  # noqa: BLE001 - coba kandidat berikutnya
                continue
        return False

    def _on_chat(self, page: Any, chat_id: str) -> bool:
        """True bila halaman sedang membuka percakapan `chat_id`."""
        return bool(chat_id) and self.current_chat_id(page) == chat_id

    def _goto(self, page: Any, url: str | None = None) -> None:
        dest = url or self.chat_url
        try:
            page.goto(dest, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            raise BrowserError(f"gagal membuka {dest}: {exc}") from exc

    def _wait_login(self, page: Any, check_cancel: Callable[[], None]) -> None:
        """Tunggu pengguna BENAR-BENAR menyelesaikan sign-in di jendela Chrome."""
        deadline = time.time() + self.login_timeout
        while time.time() < deadline:
            check_cancel()
            try:
                if page.is_closed():
                    raise BrowserError(
                        "jendela Chrome ditutup sebelum login selesai. "
                        "Pilih ulang modelnya untuk mencoba lagi."
                    )
            except BrowserError:
                raise
            except Exception:  # noqa: BLE001
                pass
            if self._chat_ready(page, 2000, check_cancel):
                return
            try:
                page.wait_for_timeout(1000)
            except Exception:  # noqa: BLE001 - page mati saat menunggu
                raise BrowserError(
                    "jendela Chrome tertutup saat menunggu login. Coba lagi."
                )
        raise BrowserError(
            "login tidak selesai dalam waktu yang ditentukan. Coba lagi."
        )

    def _on_login_page(self, page: Any) -> bool:
        """True bila page sedang di halaman login/auth (mis. /login situs, Google
        sign-in, dsb) — dipastikan lewat URL, bukan tebakan elemen."""
        try:
            url = (page.url or "").lower()
        except Exception:  # noqa: BLE001
            return False
        return any(m in url for m in self.login_url_markers)

    def _looks_logged_out(self, page: Any) -> bool:
        """True bila halaman menampilkan penanda BELUM login (mis. tombol
        "Log in") walau kotak input tetap terlihat untuk tamu."""
        if not self.logged_out_selector:
            return False
        try:
            return page.query_selector(self.logged_out_selector) is not None
        except Exception:  # noqa: BLE001
            return False

    def _looks_logged_in(self, page: Any) -> bool:
        """True bila ada BUKTI POSITIF sesi sudah login (mis. tombol profil).
        Connector yang tak menyebutkan penandanya dianggap tak berpendapat —
        True, sehingga perilakunya persis seperti sebelum bukti positif ada."""
        if not self.logged_in_selector:
            return True
        try:
            return page.query_selector(self.logged_in_selector) is not None
        except Exception:  # noqa: BLE001
            return False

    def _chat_ready(
        self,
        page: Any,
        timeout_ms: int,
        check_cancel: Callable[[], None] | None = None,
    ) -> bool:
        """Deteksi KETAT bahwa halaman chat siap & user SUDAH login:
        (1) URL BUKAN halaman login/auth, (2) tak ada penanda "belum login",
        (3) ADA penanda sudah-login bila connector menyebutkannya, dan (4) kotak
        input chat terlihat. Halaman login yang kebetulan punya elemen mirip
        input — atau halaman TAMU yang inputnya aktif tapi tak memproses pesan —
        tak akan lolos.

        Syarat (3) ada karena (2) saja TIDAK cukup: penanda logout bisa belum
        dirender saat halaman baru dibuka (lihat logged_in_selector)."""
        deadline = time.time() + timeout_ms / 1000.0
        while True:
            if check_cancel is not None:
                check_cancel()
            if not self._on_login_page(page) and not self._looks_logged_out(page) \
                    and self._looks_logged_in(page):
                for sel in self._input_selectors():
                    try:
                        el = page.query_selector(sel)
                        if el is not None and el.is_visible():
                            return True
                    except Exception:  # noqa: BLE001 - DOM/page sedang transisi
                        continue
            if time.time() >= deadline:
                return False
            try:
                page.wait_for_timeout(250)
            except Exception:  # noqa: BLE001
                return False

    def _normalize_layout(self, page: Any) -> None:
        """Pastikan situs dirender pada layout minimal yang didukung connector.

        Hanya bekerja bila jendelanya memang lebih sempit dari min_layout_*;
        pada jendela normal (apalagi termaksimalkan) tak ada yang diubah sama
        sekali, sehingga ukuran asli tetap dipakai seperti sebelumnya.

        Penggantian metrik CDP dipilih karena `viewport` Playwright tidak
        berlaku pada context ber-no_viewport. Efeknya halaman dirender pada
        ukuran yang diminta lalu diskalakan ke dalam jendela — koordinat CSS
        tetap konsisten, jadi klik & hit-test tetap benar.

        Tak perlu penjaga "sudah pernah dipasang": sesudah berhasil,
        window.innerWidth ikut melaporkan ukuran baru sehingga pemanggilan
        berikutnya berhenti di pemeriksaan pertama. Sengaja senyap — ini
        penyesuaian kenyamanan, bukan syarat kerja."""
        if not (self.min_layout_width or self.min_layout_height):
            return
        try:
            w = int(page.evaluate("() => window.innerWidth") or 0)
            h = int(page.evaluate("() => window.innerHeight") or 0)
        except Exception:  # noqa: BLE001 - halaman belum siap; coba lagi nanti
            return
        if w >= self.min_layout_width and h >= self.min_layout_height:
            return
        try:
            cdp = page.context.new_cdp_session(page)
            cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": max(w, self.min_layout_width),
                "height": max(h, self.min_layout_height),
                "deviceScaleFactor": 1,
                # WAJIB False: mode "mobile" memicu layout ponsel situs, persis
                # keadaan yang justru ingin dihindari di sini.
                "mobile": False,
            })
        except Exception:  # noqa: BLE001
            pass

    def _tampilkan_saja(self) -> bool:
        """Jendela situs ini sengaja DIBIARKAN TERLIHAT?"""
        return bool(self.show_window or config.CONNECTOR_SHOW)

    def _background(self, page: Any) -> None:
        """Jalankan browser DI LATAR: jendelanya disembunyikan sepenuhnya (tak
        muncul di taskbar), prosesnya tetap hidup & merender normal sehingga
        jawaban tetap terbaca. Pengguna cukup memakai terminal.

        Bukan headless: Cloudflare menolak sesi headless. Bila penyembunyian
        jendela tak didukung, jatuh ke minimize lewat CDP.

        DILEWATI bila situs ini ditandai show_window (atau CONNECTOR_SHOW=true):
        jendelanya justru dipastikan TERLIHAT, supaya seluruh proses menjawab
        bisa diamati langsung."""
        if self._tampilkan_saja():
            self._foreground(page)
            return
        if set_windows_visible(self.service, False):
            return
        try:  # cadangan: minimalkan lewat CDP
            cdp = page.context.new_cdp_session(page)
            info = cdp.send("Browser.getWindowForTarget")
            cdp.send("Browser.setWindowBounds", {
                "windowId": info["windowId"],
                "bounds": {"windowState": "minimized"},
            })
        except Exception:  # noqa: BLE001
            pass

    def _foreground(self, page: Any) -> None:
        """Tampilkan kembali jendela browser (dipakai saat pengguna harus login)."""
        if set_windows_visible(self.service, True):
            return
        try:
            cdp = page.context.new_cdp_session(page)
            info = cdp.send("Browser.getWindowForTarget")
            # Posisinya ikut ditentukan, bukan cuma windowState. Jendela ini
            # dilahirkan di -32000,-32000 supaya tak pernah berkelebat, dan
            # "normal" saja mengembalikannya persis ke sana — tampil menurut
            # sistem, tapi tak ada di layar pengguna.
            cdp.send("Browser.setWindowBounds", {
                "windowId": info["windowId"],
                "bounds": {"windowState": "normal", "left": 60, "top": 40,
                           "width": 1200, "height": 850},
            })
        except Exception:  # noqa: BLE001
            pass
