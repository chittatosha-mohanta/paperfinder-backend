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
        'www','page','pages','dongdong','shuhan','meizi','xiang','kemal','polat','normal'
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

def set_two_columns(section):
    sectPr = section._sectPr
    for existing in sectPr.findall(qn('w:cols')):
        sectPr.remove(existing)
    cols = OxmlElement('w:cols')
    cols.set(qn('w:num'), '2')
    cols.set(qn('w:space'), '720')
    cols.set(qn('w:equalWidth'), '1')
    sectPr.append(cols)

def insert_continuous_section_break(doc, two_col=True):
    para = doc.add_paragraph()
    pPr = para._p.get_or_add_pPr()
    sectPr = OxmlElement('w:sectPr')
    pgType = OxmlElement('w:type')
    pgType.set(qn('w:val'), 'continuous')
    sectPr.append(pgType)
    cols = OxmlElement('w:cols')
    if two_col:
        cols.set(qn('w:num'), '2')
        cols.set(qn('w:space'), '720')
        cols.set(qn('w:equalWidth'), '1')
    else:
        cols.set(qn('w:num'), '1')
    sectPr.append(cols)
    pPr.append(sectPr)

def clean_line(line):
    line = re.sub(r'\*\*(.+?)\*\*', r'\1', line)
    line = re.sub(r'\*(.+?)\*', r'\1', line)
    line = re.sub(r'#{1,6}\s*', '', line)
    return line.strip()

def is_section_heading(line, keywords):
    clean = re.sub(r'^[IVXivx]+\.\s*|^\d+\.?\s*', '', line.lower()).strip().rstrip('.')
    return any(clean.startswith(kw) for kw in keywords) and len(line) < 80


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
            return jsonify({"success": True, "query": extract_query(text), "method": "pymupdf", "preview": text[:200]})
    except Exception as e:
        print(f"PyMuPDF failed: {e}")
    try:
        text = extract_with_pdfplumber(pdf_bytes)
        if text and len(text.split()) > 15:
            return jsonify({"success": True, "query": extract_query(text), "method": "pdfplumber", "preview": text[:200]})
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
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text() + "\n"
        pages = len(doc)
        doc.close()
        text = clean_text(text)
        if not text or len(text.split()) < 10:
            raise Exception("Could not extract text from this PDF")
        return jsonify({
            "success": True,
            "text": text[:15000],
            "pages": pages
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 422


@app.route('/chat-pdf', methods=['POST'])
def chat_pdf():
    question = request.form.get('question', '').strip()
    pdf_text  = request.form.get('pdf_text', '').strip()

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

    reference_texts = []
    for key in request.files:
        if key.startswith('reference_'):
            try:
                pdf_bytes = request.files[key].read()
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                text = "".join(page.get_text() for page in doc)
                doc.close()
                reference_texts.append(clean_text(text)[:3000])
            except Exception as e:
                print(f"Failed to read reference: {e}")

    refs_summary = "\n\n".join(
        f"Reference {i+1}:\n{t}" for i, t in enumerate(reference_texts)
    ) if reference_texts else "No references provided."

    format_rules = {
        "IEEE": """IEEE FORMAT RULES:
- Section headings with Roman numerals: I. INTRODUCTION, II. RELATED WORK, III. METHODOLOGY, IV. RESULTS AND DISCUSSION, V. CONCLUSION
- Abstract: single paragraph, no heading number, starts with 'Abstract—'
- After abstract: Index Terms—keyword1, keyword2, keyword3
- Inline citations: [1], [2], [3]
- Reference format: [1] A. Author, "Title," Journal, vol. X, no. Y, pp. ZZ-ZZ, Year.
- Subsections: A. Name, B. Name""",

        "APA": """APA FORMAT RULES:
- Title centered and bold
- Abstract paragraph labeled 'Abstract' (bold)
- Keywords: Keywords: word1, word2
- In-text citations: (Author, Year)
- Reference format: Author, A. (Year). Title. Journal, vol(issue), pages. https://doi.org/xxx
- Headings centered bold (Level 1), left bold (Level 2)""",

        "ACM": """ACM FORMAT RULES:
- Numbered sections: 1. INTRODUCTION, 2. RELATED WORK
- Abstract then CCS Concepts and Keywords
- Citations: [1], [2]
- Reference format: [1] Author. Year. Title. In Proceedings...""",

        "MLA": """MLA FORMAT RULES:
- Title centered
- In-text: (Author page)
- Works Cited at end
- Reference: Author. "Title." Journal vol (Year): pages.""",

        "Nature": """Nature FORMAT RULES:
- Abstract under 150 words
- No numbered sections
- Methods section at end
- Superscript citations: text1, text2
- Reference: 1. Author, A. Title. Journal vol, pages (year).""",

        "Springer": """Springer FORMAT RULES:
- Numbered: 1 Introduction, 2 Related Work, 2.1 Subsection
- Abstract then Keywords
- Citations: [1], [2]
- Reference: 1. Author: Title. Publisher, City (Year)"""
    }

    rules = format_rules.get(paper_format, format_rules["IEEE"])

    prompt = f"""You are an expert academic paper writer. Write a complete, professional research paper in {paper_format} format.

Paper Title: {title}

Abstract provided by author:
{abstract}

Reference papers (use for citations):
{refs_summary}

{rules}

WRITE ALL SECTIONS IN ORDER:
1. Abstract (single paragraph only, no citations, clean prose)
2. Keywords / Index Terms
3. I. INTRODUCTION (or 1. Introduction)
4. II. RELATED WORK
5. III. METHODOLOGY
6. IV. RESULTS AND DISCUSSION
7. V. CONCLUSION
8. REFERENCES

REQUIREMENTS:
- Minimum 2500 words
- Detailed academic paragraphs, no bullet points in body
- Cite references as [1], [2] throughout
- Add [DIAGRAM_HERE: description] where figures fit
- Third person academic tone
- Methodology must have equations if relevant
- Results must have comparison analysis

Start writing now with the Abstract section:"""

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

    try:
        doc = Document()

        for sec in doc.sections:
            sec.top_margin    = Cm(2.5)
            sec.bottom_margin = Cm(2.5)
            sec.left_margin   = Cm(1.5)
            sec.right_margin  = Cm(1.5)
            sec.page_width    = Cm(21.59)
            sec.page_height   = Cm(27.94)

        lines = paper_content.split('\n')
        abstract_lines = []
        keyword_lines  = []
        body_lines     = []
        mode = 'before'

        body_triggers = ['introduction', 'related', 'i.', '1.', 'ii.', '2.']
        body_headings = [
            'introduction', 'related work', 'literature review',
            'methodology', 'proposed', 'method', 'approach',
            'results', 'discussion', 'experiment', 'evaluation',
            'conclusion', 'future work', 'references', 'acknowledgment'
        ]

        for raw_line in lines:
            s = raw_line.strip()
            if not s:
                continue
            lower = s.lower()
            clean = re.sub(r'^[IVXivx]+\.\s*|^\d+\.?\s*', '', lower).strip().rstrip('.')

            if clean.startswith('abstract'):
                mode = 'abstract'
                after = re.sub(r'abstract[-—:]*\s*', '', s, flags=re.IGNORECASE).strip()
                if after:
                    abstract_lines.append(after)
                continue

            if clean.startswith('keyword') or clean.startswith('index term'):
                mode = 'keywords'
                keyword_lines.append(s)
                continue

            if mode == 'abstract':
                is_next = any(clean.startswith(k) for k in body_triggers) and len(s) < 80
                if is_next:
                    mode = 'body'
                    body_lines.append(s)
                else:
                    abstract_lines.append(s)

            elif mode == 'keywords':
                is_next = any(clean.startswith(k) for k in body_triggers) and len(s) < 80
                if is_next:
                    mode = 'body'
                    body_lines.append(s)
                else:
                    keyword_lines.append(s)

            elif mode == 'body':
                body_lines.append(s)

        # Title
        tp = doc.add_paragraph()
        tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        tp.paragraph_format.space_after = Pt(10)
        tr = tp.add_run(title)
        tr.bold = True
        tr.font.size = Pt(18)

        # Format badge
        fp = doc.add_paragraph()
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        fp.paragraph_format.space_after = Pt(14)
        fr = fp.add_run(f"[ {paper_format} Format ]")
        fr.italic = True
        fr.font.size = Pt(9)

        # Abstract heading
        ah = doc.add_paragraph()
        ah.alignment = WD_ALIGN_PARAGRAPH.LEFT
        ah.paragraph_format.space_after = Pt(3)
        ahr = ah.add_run("Abstract—" if paper_format == "IEEE" else "Abstract")
        ahr.bold = True
        ahr.font.size = Pt(10)

        # Abstract body
        abs_text = ' '.join(abstract_lines).strip()
        abs_text = re.sub(r'^ABSTRACT[-—:]*\s*', '', abs_text, flags=re.IGNORECASE).strip()
        abs_text = re.sub(r'\s+', ' ', abs_text)
        if not abs_text:
            abs_text = abstract

        ap = doc.add_paragraph()
        ap.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        ap.paragraph_format.space_after = Pt(6)
        apr = ap.add_run(abs_text)
        apr.font.size = Pt(9)
        apr.italic = True

        # Keywords
        kw_text = ' '.join(keyword_lines).strip()
        kw_text = re.sub(r'\s+', ' ', kw_text)
        if kw_text:
            kp = doc.add_paragraph()
            kp.alignment = WD_ALIGN_PARAGRAPH.LEFT
            kp.paragraph_format.space_after = Pt(10)
            kpr = kp.add_run(kw_text)
            kpr.italic = True
            kpr.font.size = Pt(9)

        # Section break → two columns
        insert_continuous_section_break(doc, two_col=True)

        # Two column body
        for raw in body_lines:
            line = clean_line(raw)
            if not line:
                continue

            if is_section_heading(line, body_headings):
                h = doc.add_paragraph()
                h.alignment = WD_ALIGN_PARAGRAPH.LEFT
                h.paragraph_format.space_before = Pt(10)
                h.paragraph_format.space_after  = Pt(4)
                hr = h.add_run(line.upper() if paper_format == "IEEE" else line)
                hr.bold = True
                hr.font.size = Pt(10)

            elif '[DIAGRAM_HERE' in line.upper() or '[FIGURE' in line.upper():
                fp2 = doc.add_paragraph()
                fp2.alignment = WD_ALIGN_PARAGRAPH.CENTER
                fp2.paragraph_format.space_before = Pt(8)
                fp2.paragraph_format.space_after  = Pt(8)
                fr2 = fp2.add_run(f'[ Figure: {line} ]')
                fr2.bold = True
                fr2.italic = True
                fr2.font.size = Pt(8)

            elif re.match(r'^\[\d+\]', line):
                rp = doc.add_paragraph()
                rp.paragraph_format.left_indent       = Inches(0.3)
                rp.paragraph_format.first_line_indent = Inches(-0.3)
                rp.paragraph_format.space_after       = Pt(3)
                rr2 = rp.add_run(line)
                rr2.font.size = Pt(8)

            elif line.lower().startswith('keywords') or line.lower().startswith('index terms'):
                kp2 = doc.add_paragraph()
                kr2 = kp2.add_run(line)
                kr2.italic = True
                kr2.font.size = Pt(9)

            else:
                pp = doc.add_paragraph()
                pp.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                pp.paragraph_format.first_line_indent = Inches(0.2)
                pp.paragraph_format.space_after       = Pt(4)
                pp.paragraph_format.line_spacing      = Pt(12)
                ppr = pp.add_run(line)
                ppr.font.size = Pt(10)

        set_two_columns(doc.sections[-1])

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        doc_b64 = base64.b64encode(buf.read()).decode('utf-8')

        return jsonify({
            "success": True,
            "content": paper_content,
            "docx_base64": doc_b64,
            "filename": f"{title[:50].replace(' ', '_')}_{paper_format}.docx"
        })

    except Exception as e:
        return jsonify({"error": f"Document generation failed: {str(e)}"}), 500


if __name__ == '__main__':
    app.run(debug=True, port=8080)