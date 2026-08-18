#!/usr/bin/env python3
"""pdf_to_a4.py - dependency-free PDF page-box converter.

Python standard library only. Handles classic xref tables, xref streams,
Flate-compressed object streams, and inherited MediaBox/CropBox.

It never renders or rescales page content. It appends an incremental update
that overrides the page object with a new MediaBox/CropBox.
"""
import argparse, re, sys, zlib

NUM_RE = re.compile(rb'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?')
OBJ_RE = re.compile(rb'(?m)(\d+)\s+(\d+)\s+obj\b')

class PDFError(Exception):
    pass

def skip_ws(data, p):
    n = len(data)
    while p < n:
        if data[p] in b' \t\r\n\x00':
            p += 1
        elif data[p] == 37:
            q = data.find(b'\n', p + 1)
            p = n if q < 0 else q + 1
        else:
            break
    return p

def skip_string(data, p):
    q, depth = p + 1, 1
    while q < len(data):
        c = data[q]
        if c == 92:
            q += 2
            continue
        if c == 40:
            depth += 1
        elif c == 41:
            depth -= 1
            if depth == 0:
                return q + 1
        q += 1
    raise PDFError('unterminated PDF string')

def parse_simple_value(data, p):
    p = skip_ws(data, p)
    if p >= len(data):
        raise PDFError('unexpected EOF while parsing value')
    if data.startswith(b'<<', p):
        return parse_dict(data, p)[0], parse_dict(data, p)[1]
    if data[p:p+1] == b'[':
        depth, q = 1, p + 1
        while q < len(data) and depth:
            if data[q:q+1] == b'[':
                depth += 1
            elif data[q:q+1] == b']':
                depth -= 1
            elif data[q:q+1] == b'(':
                q = skip_string(data, q) - 1
            q += 1
        if depth:
            raise PDFError('unterminated array')
        return data[p:q], q
    if data[p:p+1] == b'/':
        q = p + 1
        while q < len(data) and data[q] not in b' \t\r\n/[]()<>{}%':
            q += 1
        return data[p:q], q
    if data[p:p+1] == b'(':
        q = skip_string(data, p)
        return data[p:q], q
    m = re.match(rb'(?:true|false|null)\b', data[p:])
    if m:
        q = p + len(m.group(0))
        return data[p:q], q
    m = NUM_RE.match(data, p)
    if m:
        q = m.end()
        q2 = skip_ws(data, q)
        m2 = NUM_RE.match(data, q2)
        if m2:
            q3 = skip_ws(data, m2.end())
            if data[q3:q3+1] == b'R':
                return data[p:q3+1], q3+1
        return data[p:q], q
    q = p
    while q < len(data) and data[q] not in b' \t\r\n/[]()<>{}%':
        q += 1
    return data[p:q], q

def parse_dict(data, p):
    if not data.startswith(b'<<', p):
        raise PDFError('expected dictionary')
    start = p
    p += 2
    items = {}
    while True:
        p = skip_ws(data, p)
        if data.startswith(b'>>', p):
            return items, p + 2, start, p + 2
        if p >= len(data):
            raise PDFError('unterminated dictionary')
        if data[p:p+1] != b'/':
            q = p
            while q < len(data) and data[q] not in b' \t\r\n':
                q += 1
            p = q
            continue
        q = p + 1
        while q < len(data) and data[q] not in b' \t\r\n/[]()<>{}%':
            q += 1
        key = data[p+1:q].decode('latin1')
        vs = skip_ws(data, q)
        value, ve = parse_simple_value(data, vs)
        items[key] = (value, vs, ve)
        p = ve

def parse_num_array(raw):
    return [float(x) for x in NUM_RE.findall(raw)]

def get_ref(raw):
    m = re.fullmatch(rb'\s*(\d+)\s+(\d+)\s+R\s*', raw)
    return (int(m.group(1)), int(m.group(2))) if m else None

def apply_png_predictor(data, colors, bpc, columns):
    bpp = max(1, (colors * bpc + 7) // 8)
    rowlen = (columns * colors * bpc + 7) // 8
    out = bytearray()
    prev = bytearray(rowlen)
    pos, n = 0, len(data)
    while pos < n:
        ft = data[pos]
        pos += 1
        row = bytearray(data[pos:pos + rowlen])
        if not row:
            break
        if len(row) < rowlen:
            row.extend(b'\x00' * (rowlen - len(row)))
        pos += rowlen
        if ft == 0:
            pass
        elif ft == 1:
            for i in range(bpp, rowlen):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif ft == 2:
            for i in range(rowlen):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(rowlen):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(rowlen):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (row[i] + pr) & 0xFF
        else:
            raise PDFError('bad PNG predictor filter type: %d' % ft)
        out += row
        prev = row
    return bytes(out)

def apply_tiff_predictor(data, colors, bpc, columns):
    if bpc != 8:
        raise PDFError('unsupported TIFF predictor bit depth: %d' % bpc)
    rowlen = columns * colors
    out = bytearray(data)
    for r in range(0, len(out) - rowlen + 1, rowlen):
        for i in range(colors, rowlen):
            out[r + i] = (out[r + i] + out[r + i - colors]) & 0xFF
    return bytes(out)

def apply_predictor(data, parms):
    pred = int(parms.get('Predictor', 1))
    if pred <= 1:
        return data
    colors = int(parms.get('Colors', 1))
    bpc = int(parms.get('BitsPerComponent', 8))
    columns = int(parms.get('Columns', 1))
    if pred == 2:
        return apply_tiff_predictor(data, colors, bpc, columns)
    if pred >= 10:
        return apply_png_predictor(data, colors, bpc, columns)
    raise PDFError('unsupported /Predictor value: %d' % pred)

def numeric_parms(raw):
    """Return {name: int} for a decode-parms dictionary (parsed dict or raw bytes)."""
    out = {}
    if isinstance(raw, dict):
        pairs = ((k, v[0]) for k, v in raw.items())
    elif isinstance(raw, (bytes, bytearray)):
        pairs = ((k.decode('latin1'), v) for k, v in re.findall(rb'/([A-Za-z0-9]+)\s+(\d+)', bytes(raw)))
    else:
        return out
    for k, v in pairs:
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            continue
    return out

def parms_list(items, count):
    """One decode-parms dict per filter, in filter order."""
    out = [{} for _ in range(max(count, 1))]
    raw = None
    for key in ('DecodeParms', 'DP'):
        if key in items:
            raw = items[key][0]
            break
    if raw is None:
        return out
    if isinstance(raw, (bytes, bytearray)) and bytes(raw).lstrip().startswith(b'['):
        i = 0
        for m in re.finditer(rb'<<.*?>>|null', bytes(raw), re.S):
            if i >= len(out):
                break
            out[i] = numeric_parms(m.group(0)) if m.group(0) != b'null' else {}
            i += 1
    else:
        out[0] = numeric_parms(raw)
    return out

def decode_filters(data, filters, parms=None):
    if not filters:
        return data
    names = [filters] if isinstance(filters, bytes) else filters
    if parms is None:
        parms = [{}] * len(names)
    for f, parm in zip(names, list(parms) + [{}] * len(names)):
        name = f.decode('latin1') if isinstance(f, bytes) else str(f)
        if name in ('FlateDecode', 'Fl'):
            try:
                data = zlib.decompress(data)
            except zlib.error:
                try:
                    data = zlib.decompressobj().decompress(data)
                except zlib.error as e:
                    raise PDFError('FlateDecode failed: ' + str(e))
            data = apply_predictor(data, parm)
        elif name in ('ASCIIHexDecode', 'AHx'):
            s = re.sub(rb'\s+', b'', data).rstrip(b'>')
            if len(s) & 1:
                s += b'0'
            data = bytes.fromhex(s.decode('ascii'))
        elif name in ('ASCII85Decode', 'A85'):
            import base64
            s = re.sub(rb'\s+', b'', data)
            if s.startswith(b'<~'):
                s = s[2:]
            if not s.endswith(b'~>'):
                s += b'~>'
            data = base64.a85decode(s, adobe=True)
        else:
            raise PDFError('unsupported PDF stream filter: ' + name)
    return data

class PDF:
    def __init__(self, data, debug=False):
        self.data = data
        self.debug = debug
        self.objects = {}       # obj -> (offset, generation)
        self.compressed = {}   # obj -> (objstm, index)
        self.trailer = {}
        self.startxref = self.find_startxref()
        self.xref_type = None
        self.parse_xref_chain(self.startxref)

    def log(self, s):
        if self.debug:
            print('[DEBUG] ' + s, file=sys.stderr)

    def find_startxref(self):
        ms = list(re.finditer(rb'(?m)^startxref\s*\r?\n\s*(\d+)', self.data))
        if not ms:
            raise PDFError('no startxref found')
        return int(ms[-1].group(1))

    def parse_xref_chain(self, off):
        seen = set()
        first_type = None
        while off is not None and off not in seen:
            seen.add(off)
            typ, prev = self.parse_xref_at(off)
            first_type = first_type or typ
            off = prev
        if off in seen:
            raise PDFError('circular /Prev xref chain')
        self.xref_type = first_type
        self.log(f'xref chain sections: {len(seen)}')
        self.log(f'normal objects: {len(self.objects)}, compressed objects: {len(self.compressed)}')

    def parse_xref_at(self, off):
        p = skip_ws(self.data, off)
        if self.data.startswith(b'xref', p):
            return self.parse_classic_at(p)
        m = OBJ_RE.match(self.data, p)
        if not m:
            raise PDFError(f'no xref stream object at offset {off}')
        obj = int(m.group(1))
        end = self.find_obj_end(m.end())
        decoded, items = self.extract_stream(m.end(), end)
        if b'/XRef' not in self.data[m.end():end] and items.get('Type', (b'',0,0))[0] != b'/XRef':
            raise PDFError('startxref does not point to xref table or /Type /XRef stream')
        W = [int(x) for x in parse_num_array(items['W'][0])]
        if len(W) != 3:
            raise PDFError('bad xref /W')
        if 'Index' in items:
            idx = [int(x) for x in parse_num_array(items['Index'][0])]
        else:
            idx = [0, int(items['Size'][0])]
        if len(idx) % 2:
            raise PDFError('bad xref /Index')
        pos = 0
        # Newer section wins, so only add entries that aren't already known.
        for k in range(0, len(idx), 2):
            first, count = idx[k], idx[k+1]
            for n in range(first, first + count):
                vals = []
                for width in W:
                    v = 0
                    for b in decoded[pos:pos+width]:
                        v = (v << 8) | b
                    pos += width
                    vals.append(v)
                typ = vals[0] if W[0] else 1
                if n in self.objects or n in self.compressed:
                    continue
                if typ == 1:
                    self.objects[n] = (vals[1], vals[2])
                elif typ == 2:
                    self.compressed[n] = (vals[1], vals[2])
        # Keep latest trailer values; only fill missing values from older sections.
        for k, v in items.items():
            self.trailer.setdefault(k, v)
        prev = None
        if 'Prev' in items:
            try:
                prev = int(items['Prev'][0])
            except Exception:
                prev = None
        self.log(f'xref stream object={obj}, W={W}, entries added from this section')
        return 'stream', prev

    def parse_classic_at(self, p):
        q = p + 4
        while True:
            q = skip_ws(self.data, q)
            if self.data.startswith(b'trailer', q):
                q += 7
                break
            m = re.match(rb'(\d+)\s+(\d+)', self.data[q:])
            if not m:
                raise PDFError('bad classic xref subsection')
            first, count = map(int, m.groups())
            q += m.end()
            q = skip_ws(self.data, q)
            for i in range(count):
                e = self.data.find(b'\n', q)
                if e < 0:
                    raise PDFError('bad classic xref entry')
                parts = self.data[q:e].strip().split()
                q = e + 1
                if len(parts) >= 3 and parts[2] == b'n':
                    n = first + i
                    if n not in self.objects and n not in self.compressed:
                        self.objects[n] = (int(parts[0]), int(parts[1]))
        q = skip_ws(data := self.data, q)
        items, _, _, _ = parse_dict(data, q)
        for k, v in items.items():
            self.trailer.setdefault(k, v)
        prev = None
        if 'Prev' in items:
            try: prev = int(items['Prev'][0])
            except Exception: pass
        self.log(f'classic xref section parsed')
        return 'classic', prev

    def find_obj_end(self, p):
        e = self.data.find(b'endobj', p)
        if e < 0:
            raise PDFError('endobj not found')
        return e + 6

    def extract_stream(self, obj_content_start, obj_end):
        chunk = self.data[obj_content_start:obj_end]
        m = re.search(rb'\bstream\r?\n', chunk)
        if not m:
            raise PDFError('object has no stream')
        dict_part = chunk[:m.start()]
        items, _, _, _ = parse_dict(dict_part, skip_ws(dict_part, 0))
        p = obj_content_start + m.end()
        length = None
        if 'Length' in items:
            raw = items['Length'][0]
            try:
                length = int(raw)
            except Exception:
                ref = get_ref(raw)
                if ref:
                    length = int(self.resolve(ref))
        if length is None:
            e = self.data.find(b'endstream', p, obj_end)
            if e < 0: raise PDFError('endstream not found')
            raw = self.data[p:e]
        else:
            raw = self.data[p:p+length]
        filters = None
        if 'Filter' in items:
            fr = items['Filter'][0]
            if fr.startswith(b'['):
                filters = re.findall(rb'/([A-Za-z0-9]+)', fr)
            elif fr.startswith(b'/'):
                filters = [fr[1:]]
        parms = parms_list(items, len(filters) if filters else 0)
        return decode_filters(raw, filters, parms), items

    def normal_bytes(self, obj):
        off, gen = self.objects[obj]
        m = OBJ_RE.match(self.data, off)
        if not m:
            raise PDFError(f'bad object header for {obj}')
        end = self.find_obj_end(m.end())
        return self.data[m.end():end-6].strip()

    def compressed_bytes(self, obj):
        objstm, index = self.compressed[obj]
        raw, items = self.stream_object(objstm)
        try:
            n = int(items['N'][0])
            first = int(items['First'][0])
        except Exception:
            raise PDFError(f'bad ObjStm {objstm}')
        nums = [int(x) for x in re.findall(rb'\d+', raw[:first])]
        if len(nums) < 2*n:
            raise PDFError(f'bad ObjStm header in {objstm}')
        pairs = list(zip(nums[0::2], nums[1::2]))
        if index >= len(pairs):
            raise PDFError(f'bad ObjStm index for object {obj}')
        start = first + pairs[index][1]
        end = first + pairs[index+1][1] if index + 1 < len(pairs) else len(raw)
        return raw[start:end].strip()

    def resolve(self, ref):
        obj, gen = ref
        if obj in self.objects:
            raw = self.normal_bytes(obj)
            # For /Length references this is expected to be a number.
            return raw
        if obj in self.compressed:
            return self.compressed_bytes(obj)
        raise PDFError(f'object {obj} not found')

    def stream_object(self, obj):
        off, gen = self.objects[obj]
        m = OBJ_RE.match(self.data, off)
        if not m: raise PDFError(f'bad stream object {obj}')
        end = self.find_obj_end(m.end())
        return self.extract_stream(m.end(), end)

    def all_object_numbers(self):
        return sorted(set(self.objects) | set(self.compressed))

    def object_bytes(self, obj):
        if obj in self.objects: return self.normal_bytes(obj)
        if obj in self.compressed: return self.compressed_bytes(obj)
        raise PDFError(f'object {obj} not found')

    def find_page_objects(self):
        pages = []
        for obj in self.all_object_numbers():
            try: b = self.object_bytes(obj)
            except Exception: continue
            if re.search(rb'/Type\s*/Page\b', b):
                pages.append(obj)
        return pages

def box_from_dict(data, name):
    m = re.search(rb'/' + name.encode() + rb'\s*\[([^\]]*)\]', data, re.S)
    if not m: return None
    vals = parse_num_array(m.group(1))
    return vals[:4] if len(vals) >= 4 else None

def parent_ref(data):
    m = re.search(rb'/Parent\s+(\d+)\s+(\d+)\s+R', data)
    return (int(m.group(1)), int(m.group(2))) if m else None

def inherited_box(pdf, page_obj, name):
    seen = set()
    obj = page_obj
    while obj not in seen:
        seen.add(obj)
        b = pdf.object_bytes(obj)
        box = box_from_dict(b, name)
        if box: return box
        ref = parent_ref(b)
        if not ref: break
        obj = ref[0]
    return None

def replace_or_add_box(page, name, box):
    repl = b'/' + name.encode() + b'[' + ('%g %g %g %g' % tuple(box)).encode() + b']'
    pat = re.compile(rb'/' + name.encode() + rb'\s*\[[^\]]*\]', re.S)
    if pat.search(page):
        return pat.sub(repl, page, count=1)
    pos = page.rfind(b'>>')
    if pos < 0: raise PDFError('page dictionary has no closing >>')
    return page[:pos] + b'\n' + repl + b'\n' + page[pos:]

def write_incremental(pdf, output, page_obj, new_page):
    gen = pdf.objects.get(page_obj, (0, 0))[1]
    # Compressed objects are referenced as generation 0 and can be superseded
    # by a normal object with the same object number/generation in an update.
    if page_obj in pdf.compressed:
        gen = 0
    size = 0
    if 'Size' in pdf.trailer:
        try:
            size = int(pdf.trailer['Size'][0])
        except Exception:
            pass
    size = max(size, page_obj + 1, max(pdf.all_object_numbers(), default=0) + 1)
    with open(output, 'wb') as f:
        f.write(pdf.data)
        if not pdf.data.endswith((b'\n', b'\r')):
            f.write(b'\n')
        obj_offset = f.tell()
        f.write(f'{page_obj} {gen} obj\n'.encode())
        f.write(new_page.strip() + b'\nendobj\n')
        xref_offset = f.tell()
        if pdf.xref_type == 'stream':
            f.write(build_xref_stream(pdf, page_obj, gen, obj_offset, xref_offset, size))
        else:
            f.write(build_classic_xref(pdf, page_obj, gen, obj_offset, size))
        f.write(b'startxref\n')
        f.write(f'{xref_offset}\n%%EOF\n'.encode())

def trailer_refs(pdf):
    out = b''
    for key in ('Root', 'Info', 'ID'):
        v = pdf.trailer.get(key)
        if v:
            out += b'/' + key.encode() + b' ' + v[0] + b'\n'
    return out

def build_classic_xref(pdf, page_obj, gen, obj_offset, size):
    out = f'xref\n{page_obj} 1\n'.encode()
    out += f'{obj_offset:010d} {gen:05d} n \n'.encode()
    out += b'trailer\n<<\n'
    out += f'/Size {size}\n'.encode()
    out += trailer_refs(pdf)
    out += f'/Prev {pdf.startxref}\n'.encode()
    out += b'>>\n'
    return out

def build_xref_stream(pdf, page_obj, gen, obj_offset, xref_offset, size):
    """Cross-reference stream for the update, so files using xref streams stay consistent."""
    xref_obj = size
    entries = sorted([(page_obj, obj_offset, gen), (xref_obj, xref_offset, 0)])
    W = (1, 4, 2)
    payload = b''
    index = []
    for num, off, g in entries:
        index += [num, 1]
        payload += b'\x01' + off.to_bytes(4, 'big') + g.to_bytes(2, 'big')
    body = b'<</Type/XRef/Size ' + str(xref_obj + 1).encode()
    body += b'/Index[' + b' '.join(str(x).encode() for x in index) + b']'
    body += b'/W[' + b' '.join(str(x).encode() for x in W) + b']\n'
    body += trailer_refs(pdf)
    body += b'/Prev ' + str(pdf.startxref).encode()
    body += b'/Length ' + str(len(payload)).encode() + b'>>\n'
    return (f'{xref_obj} 0 obj\n'.encode() + body
            + b'stream\n' + payload + b'\nendstream\nendobj\n')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('input')
    ap.add_argument('output', nargs='?')
    ap.add_argument('--debug', action='store_true', help='print PDF structure; does not print PDF content')
    a = ap.parse_args()
    output = a.output or (a.input[:-4] + '-a4.pdf' if a.input.lower().endswith('.pdf') else a.input + '-a4.pdf')
    try:
        data = open(a.input, 'rb').read()
        if not data.startswith(b'%PDF-'):
            raise PDFError('not a PDF')
        pdf = PDF(data, a.debug)
        pages = pdf.find_page_objects()
        if not pages:
            raise PDFError('no /Type /Page object found')
        page_obj = pages[0]
        page = pdf.object_bytes(page_obj)
        media = box_from_dict(page, 'MediaBox') or inherited_box(pdf, page_obj, 'MediaBox')
        crop = box_from_dict(page, 'CropBox') or inherited_box(pdf, page_obj, 'CropBox') or media
        if not media:
            raise PDFError('no MediaBox found on page or Pages ancestors')
        x0,y0,x1,y1 = media
        W,H = (595,842) if (x1-x0) <= (y1-y0) else (842,595)
        new_box = (x0, y1-H, x0+W, y1)
        new_page = replace_or_add_box(page, 'MediaBox', new_box)
        new_page = replace_or_add_box(new_page, 'CropBox', new_box)
        if a.debug:
            print(f'[DEBUG] xref type: {pdf.xref_type}', file=sys.stderr)
            print(f'[DEBUG] page object: {page_obj}', file=sys.stderr)
            print(f'[DEBUG] page compressed: {page_obj in pdf.compressed}', file=sys.stderr)
            if page_obj in pdf.compressed:
                print(f'[DEBUG] page ObjStm: {pdf.compressed[page_obj][0]}, index: {pdf.compressed[page_obj][1]}', file=sys.stderr)
            print(f'[DEBUG] page dictionary size: {len(page)} bytes', file=sys.stderr)
            print(f'[DEBUG] original MediaBox: {media}', file=sys.stderr)
            print(f'[DEBUG] effective CropBox: {crop}', file=sys.stderr)
            print(f'[DEBUG] new MediaBox/CropBox: {new_box}', file=sys.stderr)
        print(f'Original MediaBox: {media} -> new A4 MediaBox: {new_box}')
        write_incremental(pdf, output, page_obj, new_page)
        print(f'Wrote {output}')
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        if a.debug:
            import traceback; traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    main()
