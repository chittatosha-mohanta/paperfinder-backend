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

def clean_text(text):
    text = re.sub(r'[^\x20-\x7E\n]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def extract_query(text):
    stop_words = {
        'the', 'and', 'for', 'with', 'from', 'this', 'that', 'are', 'was',
        'were', 'has', 'have', 'been', 'will', 'can', 'may', 'our', 'their',
        'also', 'which', 'when', 'where', 'how', 'what', 'all', 'not', 'but',
        'its', 'than', 'more', 'some', 'such', 'each', 'both', 'only', 'very',
        'most', 'into', 'over', 'after', 'under', 'about', 'other', 'these',
        'those', 'then', 'them', 'they', 'would', 'could', 'should', 'while',
        'between', 'through', 'during', 'before', 'any', 'use', 'used', 'using',
        'based', 'show', 'shows', 'paper', 'study', 'research', 'article',
        'results', 'result', 'method', 'methods', 'data', 'model', 'models',
        'figure', 'table', 'section', 'university', 'college', 'china', 'shanghai',
        'received', 'accepted', 'published', 'copyright', 'journal', 'volume',
        'correspondence', 'academic', 'address', 'revised', 'april', 'march',
        'october', 'january', 'february', 'june', 'july', 'august', 'september',
        'november', 'december', 'email', 'doi', 'http', 'www', 'page', 'pages',
        'dongdong', 'shuhan', 'meizi', 'xiang', 'kemal', 'polat', 'normal'
    }
    words = text.split()
    keywords = [
        w for w in words
        if len(w) > 4 and w.isalpha() and w.lower() not in stop_words
    ]
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
        page = doc[page_num]
        text += page.get_text() + " "
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
    # Remove existing cols if any
    for existing in sectPr.findall(qn('w:cols')):
        sectPr.remove(existing)
    cols = OxmlElement('w:cols')
    cols.set(qn('w:num'), '2')
    cols.set(qn('w:space'), '720')
    cols.set(qn('w:equalWidth'), '1')
    sectPr.append(cols)

def set_one_column(section):
    sectPr = section._sectPr
    for existing in sectPr.findall(qn('w:cols')):
        sectPr.remove(existing)
    cols = OxmlElement('w:cols')
    cols.set(qn('w:num'), '1')
    sectPr.append(cols)

def insert_section_break(doc, two_col=True):
    """Insert a continuous section break to switch column layout"""
    para = doc.add_paragraph()
    pPr = para._p.get_or_add_pPr()
    sectPr = OxmlElement('w:sectPr')
    cols = OxmlElement('w:cols')
    if two_col:
        cols.set(qn('w:num'), '2')
        cols.set(qn('w:space'), '720')
        cols.set(qn('w:equalWidth'), '1')
    else:
        cols.set(qn('w:num'), '1')
    sectPr.append(cols)
    # continuous break type
    pgSzCopy = OxmlElement('w:type')
    pgSzCopy.set(qn('w:val'), 'continuous')
    sectPr.append(pgSzCopy)
    pPr.append(sectPr)


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
            query = extract_query(text)
            return jsonify({"success": True, "query": query, "method": "pymupdf", "preview": text[:200]})
    except Exception as e:
        print(f"PyMuPDF failed: {e}")
    try:
        text = extract_with_pdfplumber(pdf_bytes)
        if text and len(text.split()) > 15:
            query = extract_query(text)
            return jsonify({"success": True, "query": query, "method": "pdfplumber", "preview": text[:200]})
    except Exception as e:
        print(f"pdfplumber failed: {e}")
    return jsonify({"error": "Could not extract text from this PDF."}), 422


@app.route('/generate-paper', methods=['POST'])
def generate_paper():
    title = request.form.get('title', '')
    abstract = request.form.get('abstract', '')
    paper_format = request.form.get('format', 'IEEE')

    # Extract text from reference PDFs
    reference_texts = []
    for key in request.files:
        if key.startswith('reference_'):
            file = request.files[key]
            try:
                pdf_bytes = file.read()
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                text = ""
                for page in doc:
                    text += page.get_text()
                doc.close()
                reference_texts.append(clean_text(text)[:3000])
            except Exception as e:
                print(f"Failed to read reference: {e}")

    refs_summary = ""
    if reference_texts:
        refs_summary = "\n\n".join([
            f"Reference {i+1}:\n{t}" for i, t in enumerate(reference_texts)
        ])

    # Format-specific rules
    format_rules = {
        "IEEE": """IEEE FORMAT RULES:
- Section headings: I. INTRODUCTION, II. RELATED WORK, III. METHODOLOGY, IV. RESULTS AND DISCUSSION, V. CONCLUSION
- Abstract: single paragraph, no heading number, italic style
- Keywords line after abstract: Index Terms—keyword1, keyword2, keyword3
- Citations: [1], [2], [3] style inline
- References format: [1] A. Author, "Title of paper," Journal Name, vol. X, no. Y, pp. ZZ-ZZ, Month Year.
- Use Roman numerals for section numbers
- Subsections labeled: A. Subsection Name""",

        "APA": """APA FORMAT RULES:
- Title centered and bold
- Abstract labeled Abstract (bold), single paragraph
- Keywords: Keywords: word1, word2, word3
- In-text citations: (Author, Year) style
- References with hanging indent: Author, A. A. (Year). Title. Journal, vol(issue), pages.
- Section headings centered and bold""",

        "ACM": """ACM FORMAT RULES:
- Abstract followed by CCS Concepts and Keywords
- Numbered sections: 1. INTRODUCTION, 2. RELATED WORK
- Citations: [1], [2] style
- References as numbered list at end""",

        "MLA": """MLA FORMAT RULES:
- Title centered
- In-text citations: (Author page)
- Works Cited at end
- Reference: Author Last, First. Title. Journal, vol, year, pp.""",

        "Nature": """Nature FORMAT RULES:
- Short abstract under 150 words
- No numbered sections
- Methods section at end
- Citations as numbered superscripts
- References: 1. Author, A. B. Title. Journal vol, pages (year).""",

        "Springer": """Springer FORMAT RULES:
- Numbered sections: 1 Introduction, 2 Related Work, 2.1 Subsection
- Abstract followed by Keywords
- Citations: [1], [2] style
- References numbered at end"""
    }

    rules = format_rules.get(paper_format, format_rules["IEEE"])

    prompt = f"""You are an expert academic paper writer. Write a complete, professional research paper strictly following {paper_format} journal format.

Paper Title: {title}

Abstract provided by author:
{abstract}

Reference papers provided (use these for citations and context):
{refs_summary if refs_summary else "No references provided - write based on title and abstract."}

{rules}

PAPER STRUCTURE - Write ALL sections in order:
1. Abstract (single paragraph, no citations)
2. Keywords / Index Terms
3. Introduction
4. Related Work / Literature Review
5. Methodology / Proposed Method
6. Results and Discussion
7. Conclusion
8. References (properly formatted for {paper_format})

CONTENT REQUIREMENTS:
- Write minimum 2500 words of actual academic content
- Every paragraph must be detailed, technical, and academic
- Cite the provided reference papers as [1], [2] etc throughout
- Add [DIAGRAM_HERE: brief description] where figures should be inserted
- Methodology must be very detailed with any relevant equations
- Results section must include comparison analysis
- Write in third person academic tone only
- No bullet points inside paper body paragraphs
- Make the paper publishable quality

Write the complete paper now starting with the title:"""

    # Call Groq API
    try:
        client = Groq(api_key=os.environ.get('GROQ_API_KEY'))
        message = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=6000,
            messages=[{"role": "user", "content": prompt}]
        )
        paper_content = message.choices[0].message.content
    except Exception as e:
        return jsonify({"error": f"AI generation failed: {str(e)}"}), 500

    # Generate Word document
    try:
        doc = Document()

        # Page margins (IEEE style — narrow)
        for sec in doc.sections:
            sec.top_margin = Cm(2.5)
            sec.bottom_margin = Cm(2.5)
            sec.left_margin = Cm(1.5)
            sec.right_margin = Cm(1.5)
            sec.page_width = Cm(21.59)
            sec.page_height = Cm(27.94)

        # ── PARSE content into header vs body ──────────────────
        lines = paper_content.split('\n')
        abstract_lines = []
        keyword_lines = []
        body_lines = []
        mode = 'before'

        next_section_keywords = [
            'introduction', 'keyword', 'index term', 'related', 'i.', '1.'
        ]
        body_section_keywords = [
            'introduction', 'related work', 'literature review',
            'methodology', 'proposed', 'method', 'approach',
            'results', 'discussion', 'experiment', 'evaluation',
            'conclusion', 'future work', 'references',
            'acknowledgment', 'appendix'
        ]

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            lower = stripped.lower()
            clean = re.sub(r'^[IVXivx]+\.\s*|^\d+\.?\s*', '', lower).strip().rstrip('.')

            if clean.startswith('abstract'):
                mode = 'abstract'
                after = re.sub(r'abstract\s*:?\s*', '', stripped, flags=re.IGNORECASE).strip()
                if after:
                    abstract_lines.append(after)
                continue

            if clean.startswith('keyword') or clean.startswith('index term'):
                mode = 'keywords'
                keyword_lines.append(stripped)
                continue

            if mode == 'abstract':
                is_next = any(clean.startswith(k) for k in next_section_keywords) and len(stripped) < 80
                if is_next:
                    mode = 'body'
                    body_lines.append(stripped)
                else:
                    abstract_lines.append(stripped)

            elif mode == 'keywords':
                is_next = any(clean.startswith(k) for k in next_section_keywords) and len(stripped) < 80
                if is_next:
                    mode = 'body'
                    body_lines.append(stripped)
                else:
                    keyword_lines.append(stripped)

            elif mode == 'body':
                body_lines.append(stripped)

        # ── SINGLE COLUMN — Title, Abstract, Keywords ──────────

        # Title
        title_para = doc.add_paragraph()
        title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title_para.paragraph_format.space_after = Pt(10)
        t_run = title_para.add_run(title)
        t_run.bold = True
        t_run.font.size = Pt(18)

        # Format badge
        fmt_para = doc.add_paragraph()
        fmt_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        fmt_para.paragraph_format.space_after = Pt(14)
        f_run = fmt_para.add_run(f"[ {paper_format} Format ]")
        f_run.italic = True
        f_run.font.size = Pt(9)

        # Abstract heading
        abs_head = doc.add_paragraph()
        abs_head.alignment = WD_ALIGN_PARAGRAPH.LEFT
        abs_head.paragraph_format.space_after = Pt(3)
        ah_run = abs_head.add_run("Abstract" if paper_format != "IEEE" else "Abstract—")
        ah_run.bold = True
        ah_run.font.size = Pt(10)

        # Abstract text
        abs_text = ' '.join(abstract_lines).strip() or abstract
        abs_para = doc.add_paragraph()
        abs_para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        abs_para.paragraph_format.space_after = Pt(6)
        ar = abs_para.add_run(abs_text)
        ar.font.size = Pt(9)
        ar.italic = True

        # Keywords
        if keyword_lines:
            kw_para = doc.add_paragraph()
            kw_para.alignment = WD_ALIGN_PARAGRAPH.LEFT
            kw_para.paragraph_format.space_after = Pt(12)
            kr = kw_para.add_run(' '.join(keyword_lines))
            kr.italic = True
            kr.font.size = Pt(9)

        # ── CONTINUOUS SECTION BREAK → TWO COLUMNS ─────────────
        insert_section_break(doc, two_col=True)

        # ── TWO COLUMN BODY ─────────────────────────────────────
        for line in body_lines:
            if not line:
                continue

            # Clean markdown
            line = re.sub(r'\*\*(.+?)\*\*', r'\1', line)
            line = re.sub(r'\*(.+?)\*', r'\1', line)
            line = re.sub(r'#{1,6}\s*', '', line)

            clean_check = re.sub(
                r'^[IVXivx]+\.\s*|^\d+\.?\s*', '', line.lower()
            ).strip().rstrip('.')

            is_heading = (
                any(clean_check.startswith(kw) for kw in body_section_keywords)
                and len(line) < 80
            )

            if is_heading:
                h = doc.add_paragraph()
                h.alignment = WD_ALIGN_PARAGRAPH.LEFT
                h.paragraph_format.space_before = Pt(10)
                h.paragraph_format.space_after = Pt(4)
                hr = h.add_run(
                    line.upper() if paper_format == "IEEE" else line
                )
                hr.bold = True
                hr.font.size = Pt(10)

            elif '[DIAGRAM_HERE' in line.upper() or '[FIGURE' in line.upper():
                fig_para = doc.add_paragraph()
                fig_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                fig_para.paragraph_format.space_before = Pt(8)
                fig_para.paragraph_format.space_after = Pt(8)
                fr = fig_para.add_run(f'[ Figure: {line} ]')
                fr.bold = True
                fr.italic = True
                fr.font.size = Pt(8)

            elif re.match(r'^\[\d+\]', line):
                ref_para = doc.add_paragraph()
                ref_para.paragraph_format.left_indent = Inches(0.3)
                ref_para.paragraph_format.first_line_indent = Inches(-0.3)
                ref_para.paragraph_format.space_after = Pt(3)
                rr = ref_para.add_run(line)
                rr.font.size = Pt(8)

            elif line.lower().startswith('keywords') or line.lower().startswith('index terms'):
                kp = doc.add_paragraph()
                kr2 = kp.add_run(line)
                kr2.italic = True
                kr2.font.size = Pt(9)

            else:
                para = doc.add_paragraph()
                para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                para.paragraph_format.first_line_indent = Inches(0.2)
                para.paragraph_format.space_after = Pt(4)
                para.paragraph_format.line_spacing = Pt(12)
                pr = para.add_run(line)
                pr.font.size = Pt(10)

        # Apply two-column to final section
        set_two_columns(doc.sections[-1])

        # Save
        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        doc_bytes = buf.read()
        doc_b64 = base64.b64encode(doc_bytes).decode('utf-8')

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