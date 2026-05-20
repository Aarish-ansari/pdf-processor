import fitz
import math
import os
import uuid
import re
from flask import Flask, request, send_file, jsonify, render_template
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max upload

UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)


def detect_english_side(src_doc, left_fraction=0.5, sample_pages=5):
    """
    Automatically detect which half (left or right) contains English text.
    Returns: 'left' or 'right'
    
    Logic: Count ASCII (English) characters on each half.
    Whichever side has more English chars = English side.
    """
    orig_w = src_doc[0].rect.width
    half_w = orig_w * left_fraction

    left_english  = 0
    right_english = 0
    pages_to_check = min(len(src_doc), sample_pages)

    for pg_num in range(pages_to_check):
        page = src_doc[pg_num]
        blocks = page.get_text("blocks")
        for b in blocks:
            bx0, text = b[0], b[4]
            # Count English (ASCII printable) characters
            eng_count = len(re.findall(r'[a-zA-Z]', text))
            if bx0 < half_w:
                left_english += eng_count
            else:
                right_english += eng_count

    side = 'left' if left_english >= right_english else 'right'
    print(f"  Auto-detect: left_english={left_english}, right_english={right_english} → English is on {side.upper()}")
    return side


def process_pdf(input_path, output_path,
                remove_hindi=True,
                two_up=True,
                remove_padding=True,
                left_fraction=0.5,
                padding=4):

    src_doc = fitz.open(input_path)
    total_pages = len(src_doc)
    orig_w = src_doc[0].rect.width
    orig_h = src_doc[0].rect.height
    half_w = orig_w * left_fraction

    # ── Auto-detect which side is English ─────────────────────────────────────
    if remove_hindi:
        english_side = detect_english_side(src_doc, left_fraction)
        english_on_right = (english_side == 'right')
        # Crop box x-range for the English side
        if english_on_right:
            eng_x0 = half_w       # right half starts here
            eng_x1 = orig_w
        else:
            eng_x0 = 0            # left half
            eng_x1 = half_w
    else:
        english_on_right = False
        eng_x0 = 0
        eng_x1 = orig_w

    # ── Detect content bounding box (remove padding) ───────────────────────────
    if remove_padding:
        x0_vals, y0_vals, x1_vals, y1_vals = [], [], [], []
        sample = min(total_pages, 10)
        for pg_num in range(sample):
            page = src_doc[pg_num]
            blocks = page.get_text("blocks")
            for b in blocks:
                bx0, by0, bx1, by1 = b[0], b[1], b[2], b[3]
                # Only consider blocks inside the English side
                if bx0 >= eng_x0 and bx0 < eng_x1:
                    x0_vals.append(bx0)
                    y0_vals.append(by0)
                    x1_vals.append(min(bx1, eng_x1))
                    y1_vals.append(by1)

        if x0_vals:
            cx0 = max(eng_x0, min(x0_vals) - padding)
            cy0 = max(0,      min(y0_vals) - padding)
            cx1 = min(eng_x1, max(x1_vals) + padding)
            cy1 = min(orig_h, max(y1_vals) + padding)
        else:
            cx0, cy0, cx1, cy1 = eng_x0, 0, eng_x1, orig_h
    else:
        cx0, cy0, cx1, cy1 = eng_x0, 0, eng_x1, orig_h

    content_w = cx1 - cx0
    content_h = cy1 - cy0
    crop_rect  = fitz.Rect(cx0, cy0, cx1, cy1)

    print(f"  Crop rect: {crop_rect}  →  {content_w:.0f} x {content_h:.0f} pts")

    out_doc = fitz.open()

    if two_up:
        out_page_w = content_w * 2
        out_page_h = content_h
        output_page_count = math.ceil(total_pages / 2)

        for i in range(output_page_count):
            left_src  = i * 2
            right_src = i * 2 + 1
            new_page  = out_doc.new_page(width=out_page_w, height=out_page_h)

            new_page.show_pdf_page(
                fitz.Rect(0, 0, content_w, content_h),
                src_doc, left_src,
                keep_proportion=False, clip=crop_rect
            )
            if right_src < total_pages:
                new_page.show_pdf_page(
                    fitz.Rect(content_w, 0, content_w * 2, content_h),
                    src_doc, right_src,
                    keep_proportion=False, clip=crop_rect
                )
    else:
        output_page_count = total_pages
        for pg_num in range(total_pages):
            new_page = out_doc.new_page(width=content_w, height=content_h)
            new_page.show_pdf_page(
                fitz.Rect(0, 0, content_w, content_h),
                src_doc, pg_num,
                keep_proportion=False, clip=crop_rect
            )

    out_doc.save(output_path, garbage=4, deflate=True)
    out_doc.close()
    src_doc.close()

    return {
        'input_pages':  total_pages,
        'output_pages': output_page_count,
        'size_mb':      round(os.path.getsize(output_path) / (1024 * 1024), 1)
    }


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/process', methods=['POST'])
def process():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    f = request.files['file']
    if f.filename == '' or not f.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Please upload a valid PDF file'}), 400

    uid      = uuid.uuid4().hex[:8]
    filename = secure_filename(f.filename)
    inp_path = os.path.join(UPLOAD_FOLDER, f'{uid}_{filename}')
    out_path = os.path.join(OUTPUT_FOLDER, f'{uid}_output.pdf')

    f.save(inp_path)

    try:
        remove_hindi   = request.form.get('remove_hindi',   'true') == 'true'
        two_up         = request.form.get('two_up',         'true') == 'true'
        remove_padding = request.form.get('remove_padding', 'true') == 'true'

        stats = process_pdf(
            inp_path, out_path,
            remove_hindi=remove_hindi,
            two_up=two_up,
            remove_padding=remove_padding
        )
        os.remove(inp_path)
        return jsonify({'success': True, 'file_id': uid, **stats})

    except Exception as e:
        if os.path.exists(inp_path):
            os.remove(inp_path)
        return jsonify({'error': str(e)}), 500


@app.route('/download/<file_id>')
def download(file_id):
    path = os.path.join(OUTPUT_FOLDER, f'{file_id}_output.pdf')
    if not os.path.exists(path):
        return 'File not found', 404
    return send_file(path, as_attachment=True, download_name='processed.pdf')


if __name__ == '__main__':
    print("\n🚀  PDF Processor running at  http://localhost:5000\n")
    app.run(debug=False, port=5000)
