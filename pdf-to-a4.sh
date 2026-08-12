#!/usr/bin/env bash
#
# pdf-to-a4.sh  --  Put a PDF page onto an A4 sheet WITHOUT scaling the content.
#
# The page's content (e.g. a shipping label) keeps its exact original size; only
# the page (MediaBox) is enlarged to A4 and the content is anchored top-left.
# Handy for printing a small label PDF on a full A4 sheet.
#
# No external dependencies: only bash + coreutils (head/tail/wc/printf), grep, awk.
# No Ghostscript, no qpdf, no Python. Works on classic (non-compressed) PDF
# xref tables, which is what most label generators (iText/JasperReports, etc.)
# emit. It edits the /MediaBox and rebuilds the xref table so the file stays valid.
#
# Usage:  ./pdf-to-a4.sh input.pdf [output.pdf]
#         (default output: <input>-a4.pdf)
#
set -euo pipefail

in="${1:-}"
out="${2:-}"
if [[ -z "$in" ]]; then
    echo "Usage: $0 input.pdf [output.pdf]" >&2
    exit 1
fi
[[ -f "$in" ]] || { echo "Error: '$in' not found" >&2; exit 1; }
if [[ -z "$out" ]]; then
    out="${in%.pdf}-a4.pdf"
fi

# A4 in PostScript points (1/72 inch)
A4_SHORT=595
A4_LONG=842

# --- sanity: must be a classic PDF with an 'xref' table (not an xref stream) ---
if ! grep -aqE '^xref' "$in"; then
    echo "Error: this PDF uses a cross-reference stream (no classic xref table)." >&2
    echo "       This no-dependency script can't rewrite those. Sorry." >&2
    exit 1
fi

# --- locate the last classic xref table = start of the trailer section ---
# Everything before it is the object body we keep (and edit).
xref_off=$(grep -aboE '^xref' "$in" | tail -n1 | cut -d: -f1)

# --- find the (first) /MediaBox and its current values -------------------------
mb=$(grep -aboE 'MediaBox[[:space:]]*\[[^]]*\]' "$in" | head -n1 || true)
[[ -n "$mb" ]] || { echo "Error: no /MediaBox found in $in" >&2; exit 1; }
mb_off=${mb%%:*}                       # byte offset of the match
mb_str=${mb#*:}                        # the matched text: MediaBox[...]
mb_len=${#mb_str}

# extract the four numbers
nums=$(printf '%s' "$mb_str" | grep -aoE '\-?[0-9]*\.?[0-9]+' | tr '\n' ' ')
read -r x0 y0 x1 y1 <<<"$nums"

# --- compute the new A4 MediaBox, content anchored top-left --------------------
newbox=$(awk -v x0="$x0" -v y0="$y0" -v x1="$x1" -v y1="$y1" \
             -v s="$A4_SHORT" -v l="$A4_LONG" 'BEGIN{
    w = x1 - x0; h = y1 - y0;
    if (w <= h) { W = s; H = l } else { W = l; H = s }   # match orientation
    # anchor original top-left corner (x0, y1) to A4 top-left
    nx0 = x0;        ny1 = y1;
    nx1 = x0 + W;    ny0 = y1 - H;
    printf "MediaBox[%g %g %g %g]", nx0, ny0, nx1, ny1;
}')

echo "Original MediaBox: [$x0 $y0 $x1 $y1]  ->  $newbox" >&2

# --- splice the new MediaBox into the object body (binary-safe) ----------------
tmp=$(mktemp)
body=$(mktemp)
trap 'rm -f "$tmp" "$body"' EXIT

head -c "$mb_off" "$in"                       >  "$body"   # bytes before MediaBox
printf '%s' "$newbox"                          >> "$body"   # the new MediaBox
after_off=$((mb_off + mb_len))
keep_len=$((xref_off - after_off))                          # up to old xref start
tail -c "+$((after_off + 1))" "$in" | head -c "$keep_len" >> "$body"

new_startxref=$(wc -c < "$body")

# --- reuse the original trailer dictionary (Root/Info/Size/ID) -----------------
trailer_dict=$(tail -c "+$((xref_off + 1))" "$in" \
    | awk '/^trailer/{f=1;next} f&&/^startxref/{exit} f{print}')

# --- work out the object count (Size) from the trailer -------------------------
size=$(printf '%s' "$trailer_dict" | grep -aoE '/Size[[:space:]]+[0-9]+' | grep -aoE '[0-9]+' | head -n1)
[[ -n "$size" ]] || size=$(( $(grep -acE '^[0-9]+ 0 obj' "$body") + 1 ))

# --- rebuild the xref table by scanning object offsets in the new body ---------
# collect "objnum offset" pairs
offsets=$(grep -aboE '^[0-9]+ 0 obj' "$body" \
    | awk -F: '{n=$2; sub(/ .*/,"",n); print n" "$1}')

{
    cat "$body"
    printf 'xref\n0 %s\n' "$size"
    printf '0000000000 65535 f \n'                 # free object 0
    for ((i=1; i<size; i++)); do
        o=$(printf '%s\n' "$offsets" | awk -v i="$i" '$1==i{print $2; exit}')
        if [[ -n "$o" ]]; then
            printf '%010d 00000 n \n' "$o"
        else
            printf '0000000000 65535 f \n'          # missing -> free entry
        fi
    done
    printf 'trailer\n%s\nstartxref\n%s\n%%%%EOF\n' "$trailer_dict" "$new_startxref"
} > "$tmp"

mv "$tmp" "$out"
trap 'rm -f "$body"' EXIT
echo "Wrote $out (A4 page, label unscaled, anchored top-left)." >&2
