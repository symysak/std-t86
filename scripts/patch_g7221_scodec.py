from __future__ import annotations

import re
import sys
from pathlib import Path

SC_GLOBALS = '''#include "count.h"
#include <stdio.h>
#include <stdlib.h>
/* ---- STD-T86 S-Codec adaptive separation/multiplex (ARIB sec 5.3 / 5.6) ----
   sc_mode: 0 = pristine decoder, 1 = adaptive separation (read mi
   bidirectionally), 2 = adaptive-multiplex extraction (decode a standard ci
   normally, record per-region bit spans, emit mi).

   Reverse-direction convention (identified from the Ukiha off-air capture by
   correlating decoded MLT vectors against the commercial decoder's PCM,
   2026-07-18): the reverse-multiplexed stream is packed into 16-bit words
   growing from the frame tail, with bits in FORWARD order inside each word
   (Word16-based vendor implementation). Reading plain bit-reversed from
   position 319 decodes garbage; reading word-wise yields MLT correlation
   +0.999 against the commercial oracle. */
int sc_mode = 0;
static Word16 g_frame_bits[320];
static int g_fwd, g_rev, g_dir, g_sc_alt;
static int g_dirs[14], g_starts[14];   /* diagnostics (STDT86_FE_OUT) */
static Word16 g_sorted_region[14];
static int g_span[15];
#define SC_PROTECTED_BITS 190
/* map a logical reverse-position to its physical bit: 16-bit words from the
   tail, forward bit order within each word */
#define SC_REV_POS(p) (((p) & ~15) + (15 - ((p) & 15)))
static void sc_next_bit(Bit_Obj *bitobj){
    if(g_dir==0){ bitobj->next_bit = g_frame_bits[g_fwd++]; }
    else        { bitobj->next_bit = g_frame_bits[SC_REV_POS(g_rev)]; g_rev--; }
}
/* sc_mode==2: assemble mi (sec 5.3) from recorded spans, one line per frame */
static void sc_emit_mux(int number_of_regions, int consumed){
    static FILE *fp = 0;
    Word16 mi[320];
    int used[320];
    int k, j, r, st, len, dir, fwd = g_fwd, rev = 319, alt = 1;
    g_span[number_of_regions] = consumed;
    int F, alt2 = 1, trunc = 0;
    for(j=0;j<320;j++){ mi[j]=0; used[j]=0; }
    for(j=0;j<g_fwd;j++){ mi[j]=g_frame_bits[j]; used[j]=1; }  /* envelope + cat control */
    /* pass 1: total forward extent F (reverse bits must stay physically >= F) */
    F = g_fwd;
    for(k=0;k<number_of_regions;k++){
        r = g_sorted_region[k];
        len = g_span[r+1]-g_span[r];
        if(F < SC_PROTECTED_BITS){ dir=0; } else { dir=alt2; alt2^=1; }
        if(dir==0) F += len;
    }
    /* pass 2: multiplex. A reverse bit whose physical slot dips below F would
       collide with the forward stream inside a shared 16-bit word; the vendor
       encoder truncates there (observed as ~15% ran-out frames off-air), and
       the decoder noise-fills every region after its ran-out point, so stop
       multiplexing entirely at the first collision. */
    for(k=0;k<number_of_regions && !trunc;k++){
        r = g_sorted_region[k];
        st = g_span[r]; len = g_span[r+1]-g_span[r];
        if(fwd < SC_PROTECTED_BITS){ dir=0; } else { dir=alt; alt^=1; }
        if(dir==0){ for(j=0;j<len;j++){ mi[fwd+j]=g_frame_bits[st+j]; used[fwd+j]=1; } fwd+=len; }
        else{
            for(j=0;j<len;j++){
                int ph = SC_REV_POS(rev-j);
                if(ph < F){ trunc = 1; break; }
                mi[ph]=g_frame_bits[st+j]; used[ph]=1;
            }
            if(!trunc) rev-=len;
        }
    }
    /* trailing padding bits of ci fill the unused physical positions
       (the word-mapped reverse stream fragments the middle gap; the decoder
       never reads padding, so ascending physical order is fine) */
    for(j=0,k=0; j<320; j++) if(!used[j]) mi[j]=g_frame_bits[g_span[number_of_regions]+(k++)];
    if(!fp){ const char *p = getenv("STDT86_MUX_OUT"); fp = fopen(p?p:"mux_out.txt","w"); }
    for(j=0;j<320;j++) fputc(mi[j]?'1':'0',fp);
    fputc('\\n',fp); fflush(fp);
}'''


def _rep(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"patch anchor '{label}' matched {n} times (expected 1)")
    return text.replace(old, new)


def patch_decoder(path: Path) -> None:
    t = path.read_text(encoding="latin-1")
    if "sc_mode" in t:
        print(f"{path.name}: already patched, skipping")
        return

    t = _rep(t, '#include "count.h"', SC_GLOBALS, "include")

    t = _rep(
        t,
        "        decode_envelope(bitobj,",
        "        if(sc_mode){ int _i,_j; for(_i=0;_i<20;_i++){ "
        "Word16 _w=bitobj->code_word_ptr[_i]; for(_j=0;_j<16;_j++) "
        "g_frame_bits[_i*16+_j]=(_w>>(15-_j))&1; } }\n"
        "        decode_envelope(bitobj,",
        "decode_envelope",
    )

    anchor = (
        "        rate_adjust_categories(categorization_control,\n"
        "\t\t\t                   decoder_power_categories,\n"
        "\t\t\t                   decoder_category_balances);"
    )
    t = _rep(
        t,
        anchor,
        anchor
        + """

        if(sc_mode){ int _a,_b,_t; for(_a=0;_a<number_of_regions;_a++) g_sorted_region[_a]=_a;
          for(_a=0;_a<number_of_regions-1;_a++) for(_b=0;_b<number_of_regions-1-_a;_b++)
            if(decoder_power_categories[g_sorted_region[_b]]>decoder_power_categories[g_sorted_region[_b+1]]){_t=g_sorted_region[_b];g_sorted_region[_b]=g_sorted_region[_b+1];g_sorted_region[_b+1]=_t;}
          g_fwd = 320 - bitobj->number_of_bits_left; g_rev = 319; g_sc_alt = 1; }""",
        "rate_adjust",
    )

    t = _rep(t, "    Word16 j,n;", "    Word16 j,n;\n    Word16 _kk_;", "locals")

    loop_new = """    for (_kk_=0; _kk_<number_of_regions; _kk_++)
    {
        if(sc_mode==1){ region=g_sorted_region[_kk_];
            if(g_fwd < SC_PROTECTED_BITS){ g_dir=0; } else { g_dir=g_sc_alt; g_sc_alt^=1; }
            g_dirs[region]=g_dir;
            g_starts[region] = (g_dir==0) ? g_fwd : g_rev; }
        else { region=_kk_;
            if(sc_mode==2) g_span[region] = 320 - bitobj->number_of_bits_left; }
        category = (Word16)decoder_power_categories[region];"""
    for trail in (" ", ""):
        loop_old = (
            f"    for (region=0; region<number_of_regions; region++){trail}\n"
            "    {\n"
            "        category = (Word16)decoder_power_categories[region];"
        )
        if t.count(loop_old) == 1:
            t = t.replace(loop_old, loop_new)
            break
    else:
        raise SystemExit("patch anchor 'region loop' not found")

    t = _rep(
        t,
        "    \t            get_next_bit(bitobj);",
        "    \t            if(sc_mode==1) sc_next_bit(bitobj); else get_next_bit(bitobj);",
        "huffman bit",
    )
    t = _rep(
        t,
        "\t                bitobj->number_of_bits_left = sub(bitobj->number_of_bits_left,1);",
        "\t                bitobj->number_of_bits_left = sc_mode==1 ? (g_rev-g_fwd+1) "
        ": sub(bitobj->number_of_bits_left,1);",
        "huffman bits_left",
    )
    t = _rep(
        t,
        "\t\t                    get_next_bit(bitobj);",
        "\t\t                    if(sc_mode==1) sc_next_bit(bitobj); else get_next_bit(bitobj);",
        "sign bit",
    )
    t = _rep(
        t,
        "\t\t                    bitobj->number_of_bits_left = "
        "sub(bitobj->number_of_bits_left,1);",
        "\t\t                    bitobj->number_of_bits_left = sc_mode==1 ? (g_rev-g_fwd+1) "
        ": sub(bitobj->number_of_bits_left,1);",
        "sign bits_left",
    )

    t = _rep(
        t,
        """    test();
    if (ran_out_of_bits_flag)
        bitobj->number_of_bits_left = sub(bitobj->number_of_bits_left,1);""",
        """    if(sc_mode==2) sc_emit_mux(number_of_regions, 320 - bitobj->number_of_bits_left);
    if(sc_mode==1){ /* diagnostics: per-frame ran-out flag and bits left */
        const char *_p = getenv("STDT86_FE_OUT");
        if(_p){ static FILE *_fp = 0; static int _fr = 0;
            if(!_fp) _fp = fopen(_p, "w");
            { int _r; fprintf(_fp, "%d %d %d", _fr++, (int)ran_out_of_bits_flag,
                    g_rev - g_fwd + 1);
              for(_r=0;_r<number_of_regions;_r++)
                  fprintf(_fp, " %d", (int)decoder_power_categories[_r]);
              for(_r=0;_r<number_of_regions;_r++) fprintf(_fp, " %d", g_dirs[_r]);
              { int _g,_o=0; for(_g=g_fwd;_g<=g_rev;_g++) _o+=g_frame_bits[_g];
                fprintf(_fp, " %d %d %d", g_rev-g_fwd+1, _o, g_fwd);
                for(_g=0;_g<number_of_regions;_g++)
                    fprintf(_fp, " %d", (int)decoder_region_standard_deviation[_g]);
                for(_g=0;_g<number_of_regions;_g++) fprintf(_fp, " %d", g_starts[_g]); }
        { const char *_q = getenv("STDT86_MLT_OUT");
          if(_q){ static FILE *_fq = 0; int _g;
            if(!_fq) _fq = fopen(_q, "w");
            for(_g=0;_g<number_of_regions*REGION_SIZE;_g++)
                fprintf(_fq, "%d ", (int)decoder_mlt_coefs[_g]);
            fputc('\\n', _fq); fflush(_fq); } }
              fprintf(_fp, "\\n"); }
            fflush(_fp); } }
    test();
    if (ran_out_of_bits_flag)
        bitobj->number_of_bits_left = sub(bitobj->number_of_bits_left,1);
    if(sc_mode==1) bitobj->number_of_bits_left = 0;""",
        "tail check",
    )

    path.write_text(t, encoding="latin-1")
    print(f"{path.name}: patched")


def patch_decode_main(path: Path) -> None:
    s = path.read_text(encoding="latin-1")
    if "STDT86_SCODEC" in s:
        print(f"{path.name}: already patched, skipping")
        return
    m = re.search(r"int main\(int argc,char \*argv\[\]\)\s*\{", s)
    if not m:
        raise SystemExit("decode.c: main() not found (run the modern-C fix first)")
    s = (
        s[: m.end()]
        + '\n    { extern int sc_mode; const char *_e = getenv("STDT86_SCODEC");'
        + " if(_e) sc_mode=atoi(_e); }\n"
        + s[m.end() :]
    )
    if "#include <stdlib.h>" not in s:
        s = s.replace("#include <stdio.h>", "#include <stdio.h>\n#include <stdlib.h>", 1)
    path.write_text(s, encoding="latin-1")
    print(f"{path.name}: patched")


def patch_encode_main(path: Path) -> None:
    t = path.read_text(encoding="latin-1")
    if "STDT86_MLT_OUT" in t:
        print(f"{path.name}: already patched, skipping")
        return
    anchor = ("        mag_shift = samples_to_rmlt_coefs("
              "input, history, mlt_coefs, control.frame_size);")
    n = t.count(anchor)
    if n != 1:
        raise SystemExit(f"encode.c: mlt anchor matched {n}")
    t = t.replace(anchor, anchor + """
        { const char *_q = getenv("STDT86_MLT_OUT");
          if(_q){ static FILE *_fq = 0; int _g;
            if(!_fq) _fq = fopen(_q, "w");
            fprintf(_fq, "%d ", (int)mag_shift);
            for(_g=0;_g<control.frame_size;_g++) fprintf(_fq, "%d ", (int)mlt_coefs[_g]);
            fputc(10, _fq); fflush(_fq); } }""")
    if "#include <stdlib.h>" not in t:
        t = t.replace('#include <stdio.h>', '#include <stdio.h>\n#include <stdlib.h>', 1)
    path.write_text(t, encoding="latin-1")
    print(f"{path.name}: patched (MLT dump)")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    src = Path(sys.argv[1])
    patch_decoder(src / "decoder.c")
    patch_decode_main(src / "decode.c")
    patch_encode_main(src / "encode.c")


if __name__ == "__main__":
    main()
