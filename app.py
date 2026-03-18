from groq import Groq
import os
import base64
from docx import Document
from docx.shared import Pt, Inches, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import io
import re
from flask import Flask, request, jsonify
from flask_cors import CORS
import fitz
import pdfplumber

app = Flask(__name__)
CORS(app, origins=[
    "http://localhost:5173",
    "http://localhost:5174",
    "https://paperfinder-pro.vercel.app"
])

# ── Helper functions ─────────────────────────────────────────

def clean_text(text):
    text = re.sub(r'[^\x20-\x7E\n]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def extract_query(text):
    stop_words = {
        'the','and','for','with','from','this','that','are','was','were','has',
        'have','been','will','can','may','our','their','also','which','when',
        'where','how','what','all','not','but','its','than','more','some','such',
        'each','both','only','very','most','into','over','after','under','about',
        'other','these','those','then','them','they','would','could','should',
        'while','between','through','during','before','any','use','used','using',
        'based','show','shows','paper','study','research','article','results',
        'result','method','methods','data','model','models','figure','table',
        'section','university','college','china','shanghai','received','accepted',
        'published','copyright','journal','volume','correspondence','academic',
        'address','revised','april','march','october','january','february','june',
        'july','august','september','november','december','email','doi','http',
        'www','page','pages'
    }
    words = text.split()
    keywords = [w for w in words if len(w) > 4 and w.isalpha() and w.lower() not in stop_words]
    seen = set()
    unique_keywords = []
    for w in keywords:
        lower = w.lower()
        if lower not in seen:
            seen.add(lower)
            unique_keywords.append(w)
        if len(unique_keywords) == 12:
            break
    return ' '.join(unique_keywords)

def extract_with_pymupdf(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = ""
    for page_num in range(min(2, len(doc))):
        text += doc[page_num].get_text() + " "
    doc.close()
    return clean_text(text)

def extract_with_pdfplumber(pdf_bytes):
    text = ""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[:2]:
            page_text = page.extract_text()
            if page_text:
                text += page_text + " "
    return clean_text(text)

# ── Document helpers ──────────────────────────────────────────

def set_run_font(run, size_pt, bold=False, italic=False, font_name='Times New Roman'):
    """Apply consistent Times New Roman font to a run."""
    run.font.name  = font_name
    run.font.size  = Pt(size_pt)
    run.bold       = bold
    run.italic     = italic

def set_two_columns(section):
    """Apply two-column layout to a section."""
    sectPr = section._sectPr
    for existing in sectPr.findall(qn('w:cols')):
        sectPr.remove(existing)
    cols = OxmlElement('w:cols')
    cols.set(qn('w:num'),        '2')
    cols.set(qn('w:space'),      '720')   # 0.5 inch gap
    cols.set(qn('w:equalWidth'), '1')
    sectPr.append(cols)

def insert_continuous_section_break(doc, two_col=True):
    """
    Insert a zero-height continuous section break to switch column layout
    without adding visible whitespace.
    """
    para = doc.add_paragraph()
    pf   = para.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after  = Pt(0)
    pf.line_spacing = Pt(1)

    pPr    = para._p.get_or_add_pPr()
    sectPr = OxmlElement('w:sectPr')
    pgType = OxmlElement('w:type')
    pgType.set(qn('w:val'), 'continuous')
    sectPr.append(pgType)

    cols = OxmlElement('w:cols')
    if two_col:
        cols.set(qn('w:num'),        '2')
        cols.set(qn('w:space'),      '720')
        cols.set(qn('w:equalWidth'), '1')
    else:
        cols.set(qn('w:num'), '1')
    sectPr.append(cols)
    pPr.append(sectPr)

def clean_line(line):
    """Strip markdown formatting from a line."""
    line = re.sub(r'\*\*(.+?)\*\*', r'\1', line)
    line = re.sub(r'\*(.+?)\*',     r'\1', line)
    line = re.sub(r'#{1,6}\s*',     '',    line)
    return line.strip()

def is_ieee_main_heading(line):
    """
    Detects IEEE main section headings:
      I. INTRODUCTION  /  II. RELATED WORK  /  REFERENCES  etc.
    """
    s = line.strip().upper()
    if re.match(r'^[IVX]+\.\s+[A-Z][A-Z\s]+$', s):
        return True
    if s in ('REFERENCES', 'ACKNOWLEDGMENT', 'ACKNOWLEDGMENTS',
             'DATA AVAILABILITY', 'CONFLICTS OF INTEREST'):
        return True
    return False

def is_ieee_subsection_heading(line):
    """Detects IEEE subsection headings: A. Name, B. Another Name"""
    s = line.strip()
    return bool(re.match(r'^[A-Z]\.\s+[A-Z][a-zA-Z]', s)) and len(s) < 80

def is_generic_heading(line, keywords):
    """Fallback heading detector for non-IEEE formats."""
    s     = line.strip()
    lower = s.lower()
    clean = re.sub(r'^[IVXivx]+\.\s*|^\d+[\.\s]+', '', lower).strip().rstrip('.')
    return any(clean.startswith(kw) for kw in keywords) and len(s) < 90

def is_reference_line(line):
    return bool(re.match(r'^\[\d+\]', line.strip()))

def is_figure_placeholder(line):
    upper = line.upper()
    return '[DIAGRAM_HERE' in upper or '[FIGURE' in upper

# ── Routes ───────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "message": "PaperFinder backend running"})


@app.route('/extract-pdf', methods=['POST'])
def extract_pdf():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files['file']
    if not file.filename.endswith('.pdf'):
        return jsonify({"error": "File must be a PDF"}), 400
    pdf_bytes = file.read()
    try:
        text = extract_with_pymupdf(pdf_bytes)
        if text and len(text.split()) > 15:
            return jsonify({"success": True, "query": extract_query(text),
                            "method": "pymupdf", "preview": text[:200]})
    except Exception as e:
        print(f"PyMuPDF failed: {e}")
    try:
        text = extract_with_pdfplumber(pdf_bytes)
        if text and len(text.split()) > 15:
            return jsonify({"success": True, "query": extract_query(text),
                            "method": "pdfplumber", "preview": text[:200]})
    except Exception as e:
        print(f"pdfplumber failed: {e}")
    return jsonify({"error": "Could not extract text from this PDF."}), 422


@app.route('/extract-pdf-full', methods=['POST'])
def extract_pdf_full():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files['file']
    if not file.filename.endswith('.pdf'):
        return jsonify({"error": "File must be a PDF"}), 400
    pdf_bytes = file.read()
    try:
        ref_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text    = ""
        for page in ref_doc:
            text += page.get_text() + "\n"
        pages = len(ref_doc)
        ref_doc.close()
        text = clean_text(text)
        if not text or len(text.split()) < 10:
            raise Exception("Could not extract text from this PDF")
        return jsonify({"success": True, "text": text[:15000], "pages": pages})
    except Exception as e:
        return jsonify({"error": str(e)}), 422


@app.route('/chat-pdf', methods=['POST'])
def chat_pdf():
    question = request.form.get('question', '').strip()
    pdf_text  = request.form.get('pdf_text',  '').strip()
    if not question:
        return jsonify({"error": "No question provided"}), 400
    if not pdf_text:
        return jsonify({"error": "No PDF text provided"}), 400

    prompt = f"""You are a helpful research assistant. A user has uploaded a PDF and wants to ask questions about it.

PDF CONTENT:
{pdf_text[:8000]}

USER QUESTION:
{question}

INSTRUCTIONS:
- Answer ONLY based on the PDF content above
- Be specific and reference relevant sections when helpful
- If the answer is not found in the PDF, clearly say so
- Keep answers clear, concise and academic
- Format your answer with proper paragraphs
- If the question asks for a summary, provide a structured summary

Answer:"""

    try:
        client = Groq(api_key=os.environ.get('GROQ_API_KEY'))
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        answer = response.choices[0].message.content
        return jsonify({"success": True, "answer": answer})
    except Exception as e:
        return jsonify({"error": f"AI failed: {str(e)}"}), 500


@app.route('/generate-paper', methods=['POST'])
def generate_paper():
    title        = request.form.get('title', '')
    abstract     = request.form.get('abstract', '')
    paper_format = request.form.get('format', 'IEEE')

    # Cap references at 5
    reference_texts = []
    for key in sorted(request.files.keys()):
        if key.startswith('reference_') and len(reference_texts) < 5:
            try:
                pdf_bytes = request.files[key].read()
                ref_doc   = fitz.open(stream=pdf_bytes, filetype="pdf")
                text      = "".join(page.get_text() for page in ref_doc)
                ref_doc.close()
                reference_texts.append(clean_text(text)[:3000])
            except Exception as e:
                print(f"Failed to read reference: {e}")

    refs_summary = "\n\n".join(
        f"Reference {i+1}:\n{t}" for i, t in enumerate(reference_texts)
    ) if reference_texts else "No references provided."

    # ── Strict format rules ───────────────────────────────────
    format_rules = {

        "IEEE": """STRICT IEEE JOURNAL FORMAT — FOLLOW EXACTLY:

OUTPUT STRUCTURE (write in this exact order):

━━━ ABSTRACT ━━━
Start the very first line with exactly:
Abstract—
(the word Abstract followed IMMEDIATELY by an em dash —, then the text continues on the SAME LINE)
Example: Abstract—This paper proposes a novel framework for deep learning...
- Single paragraph, 150-250 words
- NO citations, NO bullet points, NO line breaks inside the paragraph
- Do NOT write "Abstract:" or "Abstract -" — ONLY "Abstract—"

━━━ INDEX TERMS ━━━
Immediately after abstract, on its own line:
Index Terms—term one, term two, term three, term four, term five.

━━━ SECTION HEADINGS ━━━
Write ALL main section headings EXACTLY like this:
I. INTRODUCTION
II. RELATED WORK
III. METHODOLOGY
IV. RESULTS AND DISCUSSION
V. CONCLUSION
REFERENCES
(Roman numeral + period + space + ALL CAPS text — no bold markers, no markdown symbols)

━━━ SUBSECTION HEADINGS ━━━
A. Subsection Name Here
B. Another Subsection
(Capital letter + period + space + Title Case — NOT all caps)

━━━ BODY TEXT RULES ━━━
- Write in full academic paragraphs — NEVER use bullet points, dashes, or numbered lists in body
- Every paragraph: minimum 4 sentences, formally written
- In-text citations: [1], [2], [1]-[3] — always square brackets
- Add figure placeholders as: [DIAGRAM_HERE: describe what this figure shows]
- Equations go on their own line: equation_expression   (1)

━━━ REFERENCES ━━━
Each reference on its own line:
[1] A. B. Author and C. D. Author, "Title of paper," Journal Name, vol. X, no. Y, pp. ZZ-ZZ, Mon. Year.
[2] A. B. Author, Title of Book. City: Publisher, Year.
[3] A. B. Author, "Title," in Proc. Conf. Name, City, Year, pp. ZZ-ZZ.

ABSOLUTE RULES — NEVER BREAK:
NO markdown: no **bold**, no *italic*, no ## headings anywhere
NO bullet points or list items in body text
NO "Abstract:" — only "Abstract—" with em dash
NO "1. Introduction" style — only Roman numeral "I. INTRODUCTION"
Minimum 2500 words total
Third person academic voice throughout""",

        "APA": """STRICT APA 7th EDITION FORMAT:
Abstract label bold on own line, then paragraph. Keywords: word1, word2.
Level 1 headings: bold, centered, title case. Level 2: bold, left, title case.
In-text: (Author, Year) — NEVER [1]. References alphabetical, hanging indent.
Journal: Author, A. B. (Year). Title. Journal, vol(issue), pages. https://doi.org/xxx
Minimum 2500 words, no bullet points in body.""",

        "ACM": """STRICT ACM FORMAT:
ABSTRACT label all caps. CCS CONCEPTS and KEYWORDS sections after abstract.
Sections: 1. INTRODUCTION  2. RELATED WORK  3. METHODOLOGY  4. RESULTS  5. CONCLUSION
Subsections: 3.1 Name  Citations: [1], [2]
Ref: [1] Author, A. B. Year. Title. In Proceedings. ACM, pages.
Minimum 2500 words, no bullet points in body.""",

        "MLA": """STRICT MLA 9th EDITION FORMAT:
Header block: Author / Course / Instructor / Date. Title centered.
Headings: centered, bold, title case. In-text: (Author page).
Works Cited alphabetical: Author Last, First. "Title." Journal, vol., no., Year, pp.
Minimum 2500 words, no bullet points in body.""",

        "Nature": """STRICT NATURE FORMAT:
No abstract label — start paragraph directly, 150 words max.
Sections not numbered: Introduction / Results / Discussion / Methods / References
Superscript citations: word1 word2,3 — NOT [1]. 
Ref: Author, A. B. et al. Title. Journal vol, pages (year).
Minimum 2500 words, no bullet points in body.""",

        "Springer": """STRICT SPRINGER FORMAT:
Abstract bold label, then paragraph. Keywords: word1 · word2 · word3
Sections: 1 Introduction  2 Related Work  (number space Title Case, no period)
Subsections: 2.1 Name  Citations: [1], [2]
Ref: 1. Author, A.B.: Title. Journal 45(3), 123-145 (2020).
Minimum 2500 words, no bullet points in body."""
    }

    rules = format_rules.get(paper_format, format_rules["IEEE"])

    prompt = f"""You are an expert academic paper writer. Write a complete, professional, publication-ready research paper in {paper_format} format.

Paper Title: {title}

Abstract provided by author:
{abstract}

Reference papers to cite:
{refs_summary}

{rules}

Start writing immediately with the Abstract. Do NOT write the title again. Do NOT add any preamble.
Do NOT use any markdown formatting symbols (no **, no *, no ##).
Write minimum 2500 words. All body text must be in flowing academic paragraphs with no bullet points."""

    try:
        client = Groq(api_key=os.environ.get('GROQ_API_KEY'))
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=6000,
            messages=[{"role": "user", "content": prompt}]
        )
        paper_content = response.choices[0].message.content
    except Exception as e:
        return jsonify({"error": f"AI generation failed: {str(e)}"}), 500

    # ════════════════════════════════════════════════════════
    # BUILD .docx
    # ════════════════════════════════════════════════════════
    try:
        doc = Document()

        # US Letter page, IEEE-standard margins
        for sec in doc.sections:
            sec.top_margin    = Cm(2.54)
            sec.bottom_margin = Cm(2.54)
            sec.left_margin   = Cm(1.91)
            sec.right_margin  = Cm(1.91)
            sec.page_width    = Cm(21.59)
            sec.page_height   = Cm(27.94)

        # ── Parse AI output ───────────────────────────────────
        lines          = paper_content.split('\n')
        abstract_lines = []
        keyword_lines  = []
        body_lines     = []
        mode           = 'before'

        body_triggers = [
            'i. ', 'ii. ', 'iii. ', 'iv. ', 'v. ',
            'introduction', 'related', 'methodology',
            '1. ', '2. ', '1 ', '2 '
        ]
        body_headings = [
            'introduction', 'related work', 'literature review',
            'methodology', 'proposed', 'method', 'approach',
            'results', 'discussion', 'experiment', 'evaluation',
            'conclusion', 'future work', 'references',
            'acknowledgment', 'acknowledgments',
            'data availability', 'conflicts of interest'
        ]

        for raw_line in lines:
            s     = raw_line.strip()
            if not s:
                continue
            lower = s.lower()

            # Detect abstract start
            if re.match(r'^abstract[—\-:]*\s*', lower) and mode == 'before':
                mode  = 'abstract'
                after = re.sub(r'^abstract[—\-:]*\s*', '', s, flags=re.IGNORECASE).strip()
                if after:
                    abstract_lines.append(after)
                continue

            # Detect keywords / index terms
            if re.match(r'^(index terms?|keywords?)[—\-:]*', lower):
                mode = 'keywords'
                keyword_lines.append(s)
                continue

            if mode == 'abstract':
                is_body = (
                    is_ieee_main_heading(s) or
                    is_ieee_main_heading(s.upper()) or
                    any(lower.startswith(t) for t in body_triggers) and len(s) < 90
                )
                if is_body:
                    mode = 'body'
                    body_lines.append(s)
                else:
                    abstract_lines.append(s)

            elif mode == 'keywords':
                is_body = (
                    is_ieee_main_heading(s) or
                    is_ieee_main_heading(s.upper()) or
                    any(lower.startswith(t) for t in body_triggers) and len(s) < 90
                )
                if is_body:
                    mode = 'body'
                    body_lines.append(s)
                else:
                    keyword_lines.append(s)

            elif mode == 'body':
                body_lines.append(s)

        # ════════════════════════════════════════════════════
        # WRITE DOCUMENT
        # ════════════════════════════════════════════════════

        # ── Title ─────────────────────────────────────────────
        tp = doc.add_paragraph()
        tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        tp.paragraph_format.space_before = Pt(0)
        tp.paragraph_format.space_after  = Pt(6)
        tr = tp.add_run(title)
        set_run_font(tr, size_pt=16, bold=True)

        # ── Format badge ──────────────────────────────────────
        fp = doc.add_paragraph()
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        fp.paragraph_format.space_before = Pt(0)
        fp.paragraph_format.space_after  = Pt(10)
        fr = fp.add_run(f"[ {paper_format} Format ]")
        set_run_font(fr, size_pt=9, italic=True)

        # ── Abstract ──────────────────────────────────────────
        abs_text = ' '.join(abstract_lines).strip()
        abs_text = re.sub(r'^abstract[—\-:]*\s*', '', abs_text, flags=re.IGNORECASE).strip()
        abs_text = re.sub(r'\s+', ' ', abs_text)
        if not abs_text:
            abs_text = abstract

        if paper_format == "IEEE":
            # FIX 1: "Abstract—text" all on ONE paragraph, NOT italic
            abs_para = doc.add_paragraph()
            abs_para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            abs_para.paragraph_format.space_before = Pt(0)
            abs_para.paragraph_format.space_after  = Pt(4)

            r_label = abs_para.add_run("Abstract")
            set_run_font(r_label, size_pt=9, bold=True)

            r_dash  = abs_para.add_run("\u2014")   # em dash —
            set_run_font(r_dash, size_pt=9, bold=True)

            r_body  = abs_para.add_run(abs_text)
            set_run_font(r_body, size_pt=9, bold=False, italic=False)  # NOT italic

        else:
            ah  = doc.add_paragraph()
            ah.alignment = WD_ALIGN_PARAGRAPH.LEFT
            ah.paragraph_format.space_after = Pt(2)
            ahr = ah.add_run("Abstract")
            set_run_font(ahr, size_pt=10, bold=True)

            ap  = doc.add_paragraph()
            ap.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            ap.paragraph_format.space_after = Pt(6)
            apr = ap.add_run(abs_text)
            set_run_font(apr, size_pt=10, italic=True)

        # ── Index Terms / Keywords ────────────────────────────
        # FIX 2: "Index Terms" bold, em dash, then terms NOT italic — on one para
        kw_raw = ' '.join(keyword_lines).strip()
        kw_raw = re.sub(r'\s+', ' ', kw_raw)

        if kw_raw:
            kp = doc.add_paragraph()
            kp.alignment = WD_ALIGN_PARAGRAPH.LEFT
            kp.paragraph_format.space_before = Pt(0)
            kp.paragraph_format.space_after  = Pt(10)

            if paper_format == "IEEE":
                terms = re.sub(r'^index terms?[—\-:]*\s*', '', kw_raw, flags=re.IGNORECASE).strip()
                terms = re.sub(r'^keywords?[—\-:]*\s*',   '', terms,   flags=re.IGNORECASE).strip()

                r_kl = kp.add_run("Index Terms")
                set_run_font(r_kl, size_pt=9, bold=True)

                r_kd = kp.add_run("\u2014")
                set_run_font(r_kd, size_pt=9, bold=True)

                r_kt = kp.add_run(terms)
                set_run_font(r_kt, size_pt=9, italic=True)
            else:
                r_kw = kp.add_run(kw_raw)
                set_run_font(r_kw, size_pt=9, italic=True)

        # ── Section break → two-column body ──────────────────
        # FIX 3: zero-height break paragraph — no visible gap
        insert_continuous_section_break(doc, two_col=True)

        # ── Body ──────────────────────────────────────────────
        for raw in body_lines:
            line = clean_line(raw)
            if not line:
                continue

            # IEEE main heading: I. INTRODUCTION etc.
            if is_ieee_main_heading(line) or is_ieee_main_heading(line.upper()):
                # Normalise to proper IEEE heading format
                normalised = re.sub(
                    r'^([ivx]+)\.\s+(.+)',
                    lambda m: m.group(1).upper() + '. ' + m.group(2).upper(),
                    line, flags=re.IGNORECASE
                )
                if not re.match(r'^[IVX]+\.', normalised):
                    normalised = line.upper()

                h  = doc.add_paragraph()
                h.alignment = WD_ALIGN_PARAGRAPH.LEFT
                h.paragraph_format.space_before = Pt(8)
                h.paragraph_format.space_after  = Pt(3)
                hr = h.add_run(normalised)
                set_run_font(hr, size_pt=10, bold=True)

            # IEEE subsection: A. Name
            elif paper_format == "IEEE" and is_ieee_subsection_heading(line):
                h  = doc.add_paragraph()
                h.alignment = WD_ALIGN_PARAGRAPH.LEFT
                h.paragraph_format.space_before = Pt(5)
                h.paragraph_format.space_after  = Pt(2)
                hr = h.add_run(line)
                set_run_font(hr, size_pt=10, bold=True, italic=True)

            # Generic heading for non-IEEE formats
            elif is_generic_heading(line, body_headings):
                h  = doc.add_paragraph()
                h.alignment = WD_ALIGN_PARAGRAPH.LEFT
                h.paragraph_format.space_before = Pt(8)
                h.paragraph_format.space_after  = Pt(3)
                hr = h.add_run(line)
                set_run_font(hr, size_pt=10, bold=True)

            # Figure placeholder
            elif is_figure_placeholder(line):
                fp2 = doc.add_paragraph()
                fp2.alignment = WD_ALIGN_PARAGRAPH.CENTER
                fp2.paragraph_format.space_before = Pt(6)
                fp2.paragraph_format.space_after  = Pt(6)
                fr2 = fp2.add_run(f"[ Figure: {line} ]")
                set_run_font(fr2, size_pt=8, bold=True, italic=True)

            # Reference line [1] ...
            elif is_reference_line(line):
                rp = doc.add_paragraph()
                rp.alignment = WD_ALIGN_PARAGRAPH.LEFT
                # FIX 4: proper hanging indent for references
                rp.paragraph_format.left_indent       = Inches(0.3)
                rp.paragraph_format.first_line_indent = Inches(-0.3)
                rp.paragraph_format.space_before      = Pt(0)
                rp.paragraph_format.space_after       = Pt(2)
                rr = rp.add_run(line)
                set_run_font(rr, size_pt=8)

            # Equation line — ends with (n)
            elif re.search(r'\(\d+\)\s*$', line) and len(line) < 120:
                ep = doc.add_paragraph()
                ep.alignment = WD_ALIGN_PARAGRAPH.CENTER
                ep.paragraph_format.space_before = Pt(3)
                ep.paragraph_format.space_after  = Pt(3)
                er = ep.add_run(line)
                set_run_font(er, size_pt=10, italic=True)

            # Keywords repeated in body
            elif re.match(r'^(keywords?|index terms?)', line.lower()):
                kp2 = doc.add_paragraph()
                kp2.paragraph_format.space_after = Pt(4)
                kr2 = kp2.add_run(line)
                set_run_font(kr2, size_pt=9, italic=True)

            # Normal body paragraph
            else:
                pp = doc.add_paragraph()
                pp.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                # FIX 5: proper IEEE body spacing
                pp.paragraph_format.first_line_indent = Inches(0.2)
                pp.paragraph_format.space_before      = Pt(0)
                pp.paragraph_format.space_after       = Pt(3)
                pp.paragraph_format.line_spacing      = Pt(12)
                ppr = pp.add_run(line)
                set_run_font(ppr, size_pt=10)  # FIX 6: Times New Roman via set_run_font

        # Apply two-column to last section
        set_two_columns(doc.sections[-1])

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        doc_b64 = base64.b64encode(buf.read()).decode('utf-8')

        return jsonify({
            "success":     True,
            "content":     paper_content,
            "docx_base64": doc_b64,
            "filename":    f"{title[:50].replace(' ', '_')}_{paper_format}.docx"
        })

    except Exception as e:
        return jsonify({"error": f"Document generation failed: {str(e)}"}), 500


if __name__ == '__main__':
    app.run(debug=False, port=8080)