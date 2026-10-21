"""HB Paper Scout — general-purpose research paper discovery dashboard.

Install: python -m pip install streamlit requests pandas
Run:     python -m streamlit run HB_Paperscout.py
"""
from pathlib import Path
from datetime import datetime
import re, time, xml.etree.ElementTree as ET
from io import BytesIO
import pandas as pd
import requests
import streamlit as st

APP_NAME = "HB Paper Scout"
BASE = Path(__file__).resolve().parent
PDF_DIR = BASE / "downloads" / "pdfs"
REPORT_DIR = BASE / "reports"
PDF_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PMC_OA = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi"
TIMEOUT = 30
DELAY = 0.2
session = requests.Session()
session.headers.update({"User-Agent": "HB-Paper-Scout/2.0 (research discovery; polite requests)"})

st.set_page_config(page_title=APP_NAME, page_icon="📚", layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
.stApp{background:linear-gradient(145deg,#F8FAFC 0%,#EEF5F8 100%)}
[data-testid="stHeader"]{background:rgba(248,250,252,.92)}
section[data-testid="stSidebar"]{background:linear-gradient(180deg,#EAF1F6,#F5F8FB);border-right:1px solid #D6E0E9}
h1,h2,h3{color:#17324D!important;letter-spacing:-.02em}
div[data-testid="stMetric"]{background:rgba(255,255,255,.88);border:1px solid #D6E0E9;border-radius:14px;padding:13px 15px;box-shadow:0 3px 12px rgba(23,50,77,.04)}
.stButton>button,.stDownloadButton>button{background:#0D9488;color:white;border:1px solid #0D9488;border-radius:9px;font-weight:600}
.stButton>button:hover,.stDownloadButton>button:hover{background:#0F766E;border-color:#0F766E;color:white}
.hb-hero{padding:1.35rem 1.5rem;border-radius:18px;margin-bottom:1rem;background:linear-gradient(120deg,#17324D 0%,#155E75 58%,#0D9488 100%);box-shadow:0 8px 24px rgba(23,50,77,.12);color:white}
.hb-hero h1,.hb-hero p{color:white!important;margin:0}.hb-hero p{opacity:.93;padding-top:.35rem}
[data-testid="stDataFrame"]{border:1px solid #D6E0E9;border-radius:10px}a{color:#0D827A!important}
</style>""", unsafe_allow_html=True)
st.markdown(f'<div class="hb-hero"><h1>📚 {APP_NAME}</h1><p>Research paper discovery · Publication metadata · Access links · Exportable reports</p></div>', unsafe_allow_html=True)


def unique(items):
    seen=set(); out=[]
    for x in items:
        if x and x not in seen: seen.add(x); out.append(x)
    return out

def extract_pmids(text):
    return unique(re.findall(r"(?<!\d)\d{4,10}(?!\d)", str(text or "")))

def uploaded_pmids(upload):
    if not upload: return []
    raw=upload.getvalue()
    if upload.name.lower().endswith('.txt'):
        return extract_pmids(raw.decode('utf-8', errors='replace'))
    try: df=pd.read_csv(BytesIO(raw), dtype=str)
    except Exception: df=pd.read_csv(BytesIO(raw), header=None, dtype=str)
    preferred=[c for c in df.columns if 'pmid' in str(c).lower() or 'pubmed' in str(c).lower()]
    cols=preferred or list(df.columns)
    vals=[]
    for col in cols:
        for val in df[col].dropna().astype(str): vals.extend(extract_pmids(val))
    return unique(vals)

def pmcid_norm(value):
    value=str(value or '').strip().upper()
    if not value: return ''
    return value if value.startswith('PMC') else 'PMC'+value

def pmc_pdf_url(pmcid):
    p=pmcid_norm(pmcid)
    return f'https://pmc.ncbi.nlm.nih.gov/articles/{p}/pdf/' if p else ''

def safe_name(value):
    return (re.sub(r'[^\w.-]+','_',str(value or 'paper')).strip('._') or 'paper')[:110]

def lookup(pmid):
    params={'query':f'EXT_ID:{pmid} AND SRC:MED','format':'json','pageSize':1,'resultType':'core'}
    r=session.get(EPMC, params=params, timeout=TIMEOUT); r.raise_for_status()
    hits=r.json().get('resultList',{}).get('result',[])
    return hits[0] if hits else None

def oa_check(pmcid):
    p=pmcid_norm(pmcid)
    if not p: return False,'No PMCID; PMC OA package check unavailable'
    try:
        r=session.get(PMC_OA,params={'id':p},timeout=TIMEOUT); r.raise_for_status()
        root=ET.fromstring(r.text)
        err=root.find('.//error')
        if err is not None: return False,(err.text or 'Not listed by PMC OA service').strip()
        if root.find('.//record') is not None: return True,'Listed by PMC Open Access service'
        return False,'No OA package listed by PMC Open Access service'
    except Exception as e: return False,f'OA service check failed: {e}'

def author_text(rec):
    authors=rec.get('authorList',{}).get('author',[]) or []
    names=[a.get('fullName','') for a in authors if a.get('fullName')]
    return '; '.join(names[:8])+('; et al.' if len(names)>8 else '')

def keywords_text(rec):
    kws=rec.get('keywordList',{}).get('keyword',[]) or []
    return '; '.join((x.get('keyword') or x.get('term') or '') if isinstance(x,dict) else str(x) for x in kws)

def pdf_candidate_urls(pmid, pmcid, preferred_url=""):
    """Return likely direct-PDF URLs; never treat an ordinary article page as a PDF."""
    candidates = []
    if preferred_url and (
        "pdf" in preferred_url.lower()
        or preferred_url.lower().split("?")[0].endswith(".pdf")
        or "pdf=render" in preferred_url.lower()
    ):
        candidates.append(preferred_url)

    p = pmcid_norm(pmcid)
    if p:
        candidates.extend([
            f"https://pmc.ncbi.nlm.nih.gov/articles/{p}/pdf/",
            f"https://europepmc.org/articles/{p}?pdf=render",
        ])
    return list(dict.fromkeys(u for u in candidates if u))


def fetch_pdf_bytes(pmid, pmcid, title, preferred_url=""):
    """Fetch a real PDF and return bytes for Streamlit's browser-side download."""
    candidates = pdf_candidate_urls(pmid, pmcid, preferred_url)
    if not candidates:
        return None, "", "No direct PDF source was found. Open the publisher/Europe PMC page or use institutional access."

    errors = []
    for url in candidates:
        try:
            response = session.get(
                url, timeout=TIMEOUT, allow_redirects=True,
                headers={"Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.5"},
            )
            response.raise_for_status()
            content = response.content or b""
            content_type = response.headers.get("Content-Type", "").lower()
            if not content.startswith(b"%PDF-"):
                errors.append(f"{url}: response was not a PDF ({content_type or 'unknown content type'})")
                continue
            filename = safe_name(f"{pmid}_{title}") + ".pdf"
            return content, filename, f"PDF ready to download ({len(content):,} bytes)"
        except requests.RequestException as exc:
            errors.append(f"{url}: {exc}")
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    return None, "", "Could not retrieve a PDF from the available public routes. " + "; ".join(errors[:2])


def download_pdf(pmid, pmcid, title):
    """Save a successfully retrieved public PDF in the app's local downloads folder."""
    content, filename, status = fetch_pdf_bytes(pmid, pmcid, title)
    if content is None:
        return "", status
    try:
        PDF_DIR.mkdir(parents=True, exist_ok=True)
        (PDF_DIR / filename).write_bytes(content)
        return "", f"Downloaded locally: {filename}"
    except OSError as exc:
        return "", f"PDF was retrieved but could not be saved to the app folder: {exc}"

def paper_row(pmid, attempt_pdf=False):
    row={
        'PMID':pmid, 'Title':'', 'Authors':'', 'Year':'', 'Journal':'', 'DOI':'',
        'PMCID':'', 'Abstract':'', 'Keywords':'', 'Europe PMC':'No', 'PubMed':'No',
        'Open Access':'Unknown', 'PMC OA Package':'Unknown', 'PMC OA Check':'',
        'PDF Status':'Not attempted', 'PDF URL':'', 'PDF / Full Text URL':'',
        'PubMed URL':f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/',
        'Europe PMC URL':f'https://europepmc.org/article/MED/{pmid}', 'DOI URL':'',
        'Record Status':'Pending', 'Error':''
    }
    try:
        rec=lookup(pmid)
        if not rec:
            row.update({'Record Status':'Not found','Europe PMC':'No','PubMed':'Not confirmed',
                        'Error':'No matching PubMed record returned by Europe PMC'})
            row['PDF / Full Text URL'] = row['PubMed URL']
            return row

        pmcid=pmcid_norm(rec.get('pmcid',''))
        doi=rec.get('doi','') or ''
        date=rec.get('firstPublicationDate','') or ''
        epmc_oa=str(rec.get('isOpenAccess','')).upper()=='Y'
        doi_url=f'https://doi.org/{doi}' if doi else ''
        # SRC:MED lookup means this record is present in the PubMed source collection.
        row.update({
            'Title':rec.get('title','') or '', 'Authors':author_text(rec),
            'Year':date[:4] if len(date)>=4 else str(rec.get('pubYear','') or ''),
            'Journal':rec.get('journalTitle','') or '', 'DOI':doi, 'PMCID':pmcid,
            'Abstract':rec.get('abstractText','') or '', 'Keywords':keywords_text(rec),
            'Europe PMC':'Yes', 'PubMed':'Yes',
            'Open Access':'Yes (Europe PMC)' if epmc_oa else 'Not confirmed as OA',
            'DOI URL':doi_url, 'Record Status':'Found'
        })

        # Prefer an explicit PDF URL supplied by Europe PMC. If none exists,
        # use the PMC PDF endpoint when a PMCID is available.
        pdf_url=''
        fulltext_urls=(rec.get('fullTextUrlList') or {}).get('fullTextUrl',[]) or []
        for item in fulltext_urls:
            style=str(item.get('documentStyle','')).lower()
            url=item.get('url','') or ''
            if url and ('pdf' in style or url.lower().split('?')[0].endswith('.pdf') or 'pdf=render' in url.lower()):
                pdf_url=url
                break
        if not pdf_url and pmcid:
            pdf_url=pmc_pdf_url(pmcid)
        row['PDF URL']=pdf_url
        row['PDF / Full Text URL']=pdf_url or (row['Europe PMC URL'] if epmc_oa else (doi_url or row['PubMed URL']))

        if pmcid:
            listed,msg=oa_check(pmcid)
            row['PMC OA Check']=msg
            row['PMC OA Package']='Yes' if listed else 'No / not listed'
            if listed:
                row['Open Access']='Yes (PMC OA package available)'
            elif epmc_oa:
                row['PMC OA Check'] += '; Europe PMC marks article as OA'
            if attempt_pdf:
                _,row['PDF Status']=download_pdf(pmid,pmcid,row['Title'])
            else:
                row['PDF Status']='PDF link available to try; download not attempted'
        else:
            row['PMC OA Check']='No PMCID; PMC OA package check unavailable'
            row['PMC OA Package']='Not applicable (no PMCID)'
            row['PDF Status']='No direct PMC PDF; use full-text/access link'
    except requests.HTTPError as e:
        row.update({'Record Status':'Request error','Error':str(e)})
    except requests.RequestException as e:
        row.update({'Record Status':'Network error','Error':str(e)})
    except Exception as e:
        row.update({'Record Status':'Error','Error':str(e)})
    return row

def csv_bytes(df): return df.to_csv(index=False).encode('utf-8-sig')

with st.sidebar:
    st.markdown('## Search controls')
    st.caption('Version 2.0')
    max_records=st.selectbox('Maximum PMIDs per search',[25,50,100,250,500,1000],index=2)
    attempt_pdf=st.checkbox('Attempt PDF downloads',value=False,help='Attempts a public PMC PDF only when a PMCID exists. It does not bypass access restrictions.')
    st.markdown('---')
    st.markdown('### Data sources')
    st.markdown('- Europe PMC · metadata\n- NCBI PMC OA service · OA package check\n- PubMed / DOI / publisher · access links')
    st.caption('Use reasonable batch sizes and respect service and publisher terms.')

tab_search, tab_dashboard, tab_details, tab_export, tab_about = st.tabs(['🔎 Find Papers','📊 Results Dashboard','📄 Paper Details','⬇️ Export & Reports','ℹ️ About'])

with tab_search:
    st.subheader('1. Enter your PMIDs')
    st.caption('Paste identifiers or upload a TXT/CSV file. Both input methods can be used together.')
    c1,c2=st.columns([1.25,1])
    with c1:
        pasted=st.text_area('PMID list',height=205,placeholder='14729715\n42397302\n12345678',help='One PMID per line or multiple IDs separated by commas/spaces.',key='hb_pmid_text')
    with c2:
        upload=st.file_uploader('Upload PMID file',type=['txt','csv'],help='CSV files can contain a PMID or PubMed column.',key='hb_pmid_upload')
        st.info('Duplicates are removed automatically. Results include metadata, access checks, paper links, and optional PMC PDF attempts.')
    pmids=unique(extract_pmids(pasted)+uploaded_pmids(upload))
    st.metric('Unique PMIDs ready',len(pmids))
    if pmids: st.write('**Input preview:** '+', '.join(pmids[:15])+(' …' if len(pmids)>15 else ''))
    if len(pmids)>max_records: st.warning(f'{len(pmids)} PMIDs found. This run will process the first {max_records}.')
    if st.button('🔎 Search and build report',type='primary',use_container_width=True,disabled=not pmids,key='hb_run_search'):
        chosen=pmids[:max_records]; results=[]; bar=st.progress(0,text='Preparing search…'); status=st.empty()
        for i,pmid in enumerate(chosen,1):
            status.write(f'Retrieving PMID {pmid} — {i}/{len(chosen)}')
            results.append(paper_row(pmid,attempt_pdf=attempt_pdf))
            bar.progress(i/len(chosen),text=f'Processed {i} of {len(chosen)} PMIDs')
            if i<len(chosen): time.sleep(DELAY)
        bar.empty(); status.empty(); df=pd.DataFrame(results); st.session_state['hb_results']=df
        stamp=datetime.now().strftime('%Y%m%d_%H%M%S'); report=REPORT_DIR/f'HB_Paper_Scout_Report_{stamp}.csv'
        df.to_csv(report,index=False,encoding='utf-8-sig'); st.session_state['hb_report_file']=str(report)
        st.success(f'Search complete: {len(df)} PMID(s) processed. Open Results Dashboard to review them.')
    if 'hb_results' in st.session_state:
        df=st.session_state['hb_results']; a,b,c=st.columns(3)
        a.metric('Latest processed',len(df)); b.metric('Records found',int((df['Record Status']=='Found').sum())); c.metric('Records with PMCID',int(df['PMCID'].fillna('').astype(str).str.startswith('PMC').sum()))

with tab_dashboard:
    st.subheader('2. Search Results')
    if 'hb_results' not in st.session_state: st.info('Run a search in the Find Papers tab to populate this dashboard.')
    else:
        df=st.session_state['hb_results'].copy(); total=len(df)
        found=int((df['Record Status']=='Found').sum()); pmc=int(df['PMCID'].fillna('').astype(str).str.startswith('PMC').sum())
        oa=int(df['Open Access'].astype(str).str.contains('Open Access|package available',case=False,regex=True).sum())
        err=int(df['Record Status'].isin(['Error','Network error','Request error']).sum())
        m1,m2,m3,m4=st.columns(4); m1.metric('PMIDs processed',total); m2.metric('Records found',found); m3.metric('With PMCID',pmc); m4.metric('OA-labelled',oa)
        f1,f2=st.columns([1,1])
        with f1: status_filter=st.selectbox('Filter by status',['All']+sorted(df['Record Status'].dropna().unique().tolist()))
        with f2: q=st.text_input('Search title, author, journal, PMID…',key='hb_filter',placeholder='e.g. RecA or journal name')
        view=df.copy()
        if status_filter!='All': view=view[view['Record Status']==status_filter]
        if q.strip():
            cols=['PMID','Title','Authors','Journal','DOI','PMCID','Keywords']; pat=re.escape(q.strip())
            mask=view[cols].fillna('').astype(str).apply(lambda s:s.str.contains(pat,case=False,regex=True)).any(axis=1); view=view[mask]
        # Make the clickable DOI/paper link open a known PDF URL first.
        # Keep the literal DOI identifier in a separate non-clickable column.
        direct_pdf = view['PDF URL'].fillna('').astype(str)
        publisher = view['DOI URL'].fillna('').astype(str)
        view['DOI identifier'] = view['DOI'].fillna('').astype(str)
        # This clickable column intentionally opens the direct public PDF when known;
        # otherwise it falls back to the DOI/publisher page.
        view['DOI'] = direct_pdf.where(direct_pdf.str.strip() != '', publisher)
        view['Europe PMC Link'] = view['Europe PMC URL'].fillna('').astype(str)
        view['PubMed Link'] = view['PubMed URL'].fillna('').astype(str)
        view['PDF / Full Text'] = view['PDF / Full Text URL'].fillna('').astype(str)
        cols=['PMID','Title','Year','Journal','DOI identifier','DOI','Europe PMC','Europe PMC Link','PubMed','PubMed Link','Open Access','PMC OA Package','PMCID','PDF / Full Text','PDF Status','Record Status']
        st.dataframe(
            view[cols], use_container_width=True, hide_index=True,
            column_config={
                'PMID':st.column_config.TextColumn('PMID',width='small'),
                'Title':st.column_config.TextColumn('Paper title',width='large'),
                'Journal':st.column_config.TextColumn('Journal name',width='medium'),
                'DOI':st.column_config.LinkColumn('Paper PDF (DOI fallback)',display_text='Open PDF / publisher ↗', help='Opens a known direct public PDF when available; if no direct PDF URL is known, opens the DOI/publisher page.'),
                'DOI identifier':st.column_config.TextColumn('DOI identifier',width='medium'),
                'Europe PMC':st.column_config.TextColumn('In Europe PMC'),
                'Europe PMC Link':st.column_config.LinkColumn('Europe PMC record',display_text='Open Europe PMC ↗'),
                'PubMed':st.column_config.TextColumn('In PubMed'),
                'PubMed Link':st.column_config.LinkColumn('PubMed record',display_text='Open PubMed ↗'),
                'PDF / Full Text':st.column_config.LinkColumn('PDF / access route',display_text='Open PDF / access ↗', help='Opens the best available PDF or access page.'),
                'Open Access':st.column_config.TextColumn('Open access status'),
                'PMC OA Package':st.column_config.TextColumn('PMC OA package'),
            }
        )
        st.caption(f'Showing {len(view)} of {total} records. Journal name and Europe PMC/PubMed availability are shown separately. “Open PDF / publisher” opens a known direct PDF URL first and falls back to the DOI/publisher page if a direct PDF URL is not known. Use the individual download button below to verify and save an accessible PDF.')
        st.download_button('⬇️ Download filtered table (CSV)',csv_bytes(view.drop(columns=['DOI','DOI identifier','Europe PMC Link','PubMed Link','PDF / Full Text'],errors='ignore')),'HB_Paper_Scout_Filtered_Results.csv','text/csv',use_container_width=True)

        st.markdown('### Download individual papers')
        st.caption('Click “Download PDF (publisher fallback)” to try public PDF routes first. If a valid PDF is retrieved, use “Save PDF to computer”. If no public PDF can be retrieved, the app displays a publisher/DOI link as the fallback. It cannot bypass paywalls.')
        for _, paper in view.iterrows():
            pmid = str(paper.get('PMID',''))
            title = str(paper.get('Title') or 'Title unavailable')
            with st.container(border=True):
                left, right = st.columns([5, 1.35])
                with left:
                    st.markdown(f"**{title}**")
                    st.caption(f"PMID: {pmid} · {paper.get('Year') or 'Year unknown'} · Journal: {paper.get('Journal') or 'Unknown'}")
                    st.caption(f"Europe PMC: {paper.get('Europe PMC') or 'Unknown'} · PubMed: {paper.get('PubMed') or 'Unknown'} · OA: {paper.get('Open Access') or 'Unknown'}")
                    route_url = str(paper.get('PDF / Full Text URL') or '')
                    if route_url:
                        st.markdown(f"[Open PDF / full-text route ↗]({route_url})")
                    doi_url = str(paper.get('DOI URL') or '')
                    if doi_url:
                        st.markdown(f"[Paper PDF (or publisher fallback) ↗]({route_url or doi_url})")
                        st.caption(f"DOI: {paper.get('DOI') or 'Not available'}")
                    st.markdown(f"[Europe PMC record ↗](https://europepmc.org/article/MED/{pmid}) · [PubMed record ↗](https://pubmed.ncbi.nlm.nih.gov/{pmid}/)")
                    st.caption(f"PDF status: {paper.get('PDF Status') or 'Not attempted'}")
                with right:
                    pmcid = str(paper.get('PMCID') or '')
                    payload_key = f"hb_pdf_payload_{pmid}"
                    filename_key = f"hb_pdf_filename_{pmid}"
                    fallback_key = f"hb_pdf_fallback_{pmid}"
                    if fallback_key in st.session_state and st.session_state[fallback_key]:
                        st.warning('No publicly accessible PDF was retrieved. Use the publisher page below as the fallback.')
                        st.link_button('Open publisher page ↗', st.session_state[fallback_key], use_container_width=True)
                        if st.button('Try PDF again', key=f"hb_retry_pdf_{pmid}", use_container_width=True):
                            st.session_state.pop(fallback_key, None)
                            st.rerun()
                    elif payload_key in st.session_state and st.session_state[payload_key]:
                        st.download_button(
                            "💾 Save PDF to computer",
                            data=st.session_state[payload_key],
                            file_name=st.session_state.get(filename_key, f"{pmid}.pdf"),
                            mime="application/pdf",
                            key=f"hb_save_pdf_{pmid}",
                            use_container_width=True,
                        )
                        if st.button("Fetch PDF again", key=f"hb_refetch_{pmid}", use_container_width=True):
                            st.session_state.pop(payload_key, None)
                            st.session_state.pop(filename_key, None)
                            st.rerun()
                    else:
                        if st.button('⬇️ Download PDF (publisher fallback)', key=f"hb_download_{pmid}", use_container_width=True):
                            preferred = str(paper.get('PDF URL') or '')
                            with st.spinner(f'Checking public PDF sources for PMID {pmid}…'):
                                pdf_bytes, filename, result_status = fetch_pdf_bytes(
                                    pmid, pmcid, title, preferred_url=preferred
                                )
                            if pdf_bytes is not None:
                                st.session_state[payload_key] = pdf_bytes
                                st.session_state[filename_key] = filename
                                try:
                                    PDF_DIR.mkdir(parents=True, exist_ok=True)
                                    (PDF_DIR / filename).write_bytes(pdf_bytes)
                                except OSError:
                                    pass
                            else:
                                # A failed public-PDF attempt falls back to the DOI/publisher page.
                                fallback_url = str(paper.get('DOI URL') or '')
                                if not fallback_url:
                                    fallback_url = str(paper.get('Europe PMC URL') or f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/')
                                st.session_state[fallback_key] = fallback_url
                            current = st.session_state.get('hb_results', pd.DataFrame()).copy()
                            if not current.empty and 'PMID' in current.columns:
                                match = current['PMID'].astype(str) == pmid
                                current.loc[match, 'PDF Status'] = result_status
                                st.session_state['hb_results'] = current
                            st.session_state['hb_download_notice'] = f"PMID {pmid}: {result_status}"
                            st.rerun()
        if st.session_state.get('hb_download_notice'):
            st.info(st.session_state.pop('hb_download_notice'))

with tab_details:
    st.subheader('3. Paper Details and Access Links')
    if 'hb_results' not in st.session_state: st.info('Search for papers first, then inspect individual records here.')
    else:
        records=st.session_state['hb_results'].to_dict('records')
        if not records: st.info('No records to display.')
        else:
            labels=[f"{r.get('PMID','')} — {str(r.get('Title') or 'Title unavailable')[:120]}" for r in records]
            selected=records[labels.index(st.selectbox('Select a paper',labels,key='hb_detail_select'))]
            st.markdown(f"### {selected.get('Title') or 'Title unavailable'}")
            st.write(selected.get('Authors') or 'Author list unavailable')
            a,b,c=st.columns(3); a.metric('Year',selected.get('Year') or '—'); b.metric('PMID',selected.get('PMID') or '—'); c.metric('PMCID',selected.get('PMCID') or '—')
            st.markdown('#### Publication information')
            st.write(f"**Journal:** {selected.get('Journal') or 'Not available'}")
            st.write(f"**DOI:** {selected.get('DOI') or 'Not available'}")
            st.write(f"**Listed in Europe PMC:** {selected.get('Europe PMC') or 'Unknown'}")
            st.write(f"**Listed in PubMed:** {selected.get('PubMed') or 'Unknown'}")
            st.write(f"**Open-access status:** {selected.get('Open Access') or 'Unknown'}")
            st.write(f"**PMC OA package:** {selected.get('PMC OA Package') or 'Unknown'}")
            st.write(f"**PMC OA check:** {selected.get('PMC OA Check') or 'Not checked'}")
            st.write(f"**PDF status:** {selected.get('PDF Status') or 'Not attempted'}")
            if selected.get('Keywords'): st.write(f"**Keywords:** {selected['Keywords']}")
            st.markdown('#### Access links')
            x,y=st.columns(2)
            with x:
                st.markdown(f"[Open PubMed record ↗]({selected['PubMed URL']})")
                st.markdown(f"[Open Europe PMC record ↗]({selected['Europe PMC URL']})")
            with y:
                if selected.get('DOI URL'): st.markdown(f"[Open DOI / publisher page ↗]({selected['DOI URL']})")
                if selected.get('PDF / Full Text URL'): st.markdown(f"[Open PDF / full-text route ↗]({selected['PDF / Full Text URL']})")
                elif selected.get('PDF URL'): st.markdown(f"[Try PMC PDF ↗]({selected['PDF URL']})")
                elif selected.get('PMCID'): st.markdown(f"[Open PMC article ↗](https://pmc.ncbi.nlm.nih.gov/articles/{selected['PMCID']}/)")
            st.markdown('#### Abstract')
            if str(selected.get('Abstract') or '').strip(): st.write(selected['Abstract'])
            else: st.info('No abstract was supplied in the metadata response.')
            if selected.get('Error'): st.error(selected['Error'])

with tab_export:
    st.subheader('4. Export Results')
    if 'hb_results' not in st.session_state: st.info('Run a search before exporting a report.')
    else:
        df=st.session_state['hb_results'].copy()
        st.write('Download the full result set for Excel, R, Python, or other analysis.')
        st.download_button('⬇️ Export full results as CSV',csv_bytes(df),'HB_Paper_Scout_Results.csv','text/csv',use_container_width=True)
        report=st.session_state.get('hb_report_file','')
        if report: st.markdown('**Timestamped report saved locally:**'); st.code(report,language='text')
        st.markdown('**Output folders**'); st.code(f'PDF attempts: {PDF_DIR}\nCSV reports: {REPORT_DIR}',language='text')
        files=sorted(PDF_DIR.glob('*.pdf'),key=lambda p:p.stat().st_mtime,reverse=True)
        st.markdown(f'**PDFs saved by this app:** {len(files)}')
        for file in files[:20]: st.write(f'• {file.name} ({file.stat().st_size:,} bytes)')

with tab_about:
    st.subheader('About HB Paper Scout')
    st.write('HB Paper Scout is a general-purpose research paper discovery tool, not tied to a single project. It accepts PubMed identifiers, retrieves bibliographic metadata, checks PMC Open Access package availability, provides paper access links, and exports reports.')
    st.markdown('#### Workflow')
    st.markdown('1. **Input:** paste PMIDs or upload TXT/CSV.\n2. **Metadata:** retrieve title, authors, year, journal, DOI, abstract, and keywords.\n3. **Index checks:** show Europe PMC and PubMed presence separately.\n4. **Access check:** distinguish Europe PMC open-access status from PMC OA package availability.\n5. **Open / download:** the paper link opens a known public PDF first and falls back to the DOI/publisher page; each record also has separate Europe PMC and PubMed links, plus an individual PDF download option.\n6. **Export:** download CSV reports.')
    st.warning('A PMID does not guarantee a free PDF. A PMCID or Open Access label does not guarantee every PDF URL will work. If a download fails, use the publisher, PubMed, Europe PMC, or institutional library access. This app does not bypass paywalls.')
    st.markdown('#### Output structure')
    st.code('HB_Paperscout.py\ndownloads/\n  pdfs/     # successfully downloaded PMC PDFs only\nreports/    # timestamped CSV reports',language='text')

st.markdown('---')
st.caption('HB Paper Scout · General-purpose research paper discovery · Metadata source: Europe PMC')
