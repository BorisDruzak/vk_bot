"""Read-only CSV/XLSX official measurement import. No macros/formulas executed."""
from __future__ import annotations

import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

NS={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
ALIASES={
 'Количество публикаций':'posts','Лайки':'likes','Комментарии':'comments','Репосты':'reposts',
 'Просмотры':'views','ER (Вовлеченность)':'er','Достижение показателя вовлеченности, %':'p33',
 'Процент выполнивших норму публикаций':'p32','Итоговый рейтинг':'p3',
 'Отчётный период с':'period_start','Отчётный период до (исключая)':'period_end',
 'Дата замера UTC':'measured_at','ID сообщества':'group_id','Название':'name',
 'П3.3':'p33','П3.2':'p32','П3':'p3','ER':'er'}


def number(value):
    if value is None or str(value).strip() in ('','—','-','NULL'):return None
    text=str(value).strip().replace('\xa0','').replace(' ','').replace(',','.').rstrip('%')
    n=float(text)
    return int(n) if n.is_integer() else n


def group_id(url):
    match=re.fullmatch(r'https?://(?:www\.)?vk\.(?:com|ru)/(?:club|public)(\d+)/?',str(url or '').strip())
    return int(match[1]) if match else None


def workbook_rows(path):
    """Cached cell values only; bounded zip extraction; sheet names retained."""
    with zipfile.ZipFile(path) as archive:
        if sum(x.file_size for x in archive.infolist())>100_000_000:
            raise ValueError('XLSX exceeds import size limit')
        shared=[]
        if 'xl/sharedStrings.xml' in archive.namelist():
            shared=[''.join(t.text or '' for t in x.findall('.//m:t',NS))
                    for x in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('m:si',NS)]
        relns='{http://schemas.openxmlformats.org/package/2006/relationships}'
        rels={r.attrib['Id']:r.attrib['Target'] for r in ET.fromstring(archive.read('xl/_rels/workbook.xml.rels')).findall(relns+'Relationship')}
        rid='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
        book=ET.fromstring(archive.read('xl/workbook.xml'))
        for sheet in book.findall('m:sheets/m:sheet',NS):
            target=rels[sheet.attrib[rid]]
            target=target.lstrip('/') if target.startswith('/') else 'xl/'+target
            rows=[]
            for row in ET.fromstring(archive.read(target)).findall('m:sheetData/m:row',NS):
                cells={}
                for c in row.findall('m:c',NS):
                    v=c.find('m:v',NS);value=v.text if v is not None else None
                    if c.get('t')=='s' and value is not None:value=shared[int(value)]
                    elif c.get('t')=='inlineStr':value=''.join(t.text or '' for t in c.findall('.//m:t',NS))
                    cells[re.sub(r'\d','',c.attrib['r'])]=value
                rows.append((int(row.attrib['r']),cells))
            yield sheet.attrib['name'],rows


def _normalize(row):
    converted={ALIASES.get(k.strip(),k.strip()):v for k,v in row.items() if k}
    numeric=('group_id','posts','likes','comments','reposts','views','er','p33','p32','p3','p31_ratio')
    for key in numeric:
        if key in converted:converted[key]=number(converted[key])
    if converted.get('group_id') is None and converted.get('url'):
        converted['group_id']=group_id(converted['url'])
    if 'Наименование учреждения' in converted:
        gid=group_id(converted.get('Ссылка'))
        if gid is None:raise ValueError('Official institution needs explicit numeric community ID')
        converted.update(group_id=gid,name=converted['Наименование учреждения'])
    return converted


def read_official(path,*,municipality='Сосновский МО',start=None,end=None,as_of=None):
    path=Path(path)
    if path.suffix.lower()=='.csv':
        text=path.read_text(encoding='utf-8-sig')
        try:delimiter=csv.Sniffer().sniff(text[:8000],delimiters=';,\t').delimiter
        except csv.Error:delimiter=';'
        records=[_normalize(r) for r in csv.DictReader(io.StringIO(text),delimiter=delimiter)]
    elif path.suffix.lower()=='.xlsx':
        records=[]
        for sheet,rows in workbook_rows(path):
            if not rows:continue
            headers=rows[0][1]
            # Official curated per-institution sheet and district summary only.
            # Raw dashboard sheet may contain duplicated historical institutions.
            official_layout='Наименование учреждения' in headers.values() or 'Всего пабликов' in headers.values()
            if official_layout:
                for rowno,cells in rows[1:]:
                    r={title:cells.get(col) for col,title in headers.items() if title}
                    if r.get('Муниципальное образование')!=municipality:continue
                    normalized=_normalize(r)
                    if 'Наименование учреждения' in r:
                        gid=group_id(r.get('Ссылка'))
                        if gid is None:raise ValueError('Official institution URL requires explicit numeric community ID')
                        normalized.update(group_id=gid,name=r['Наименование учреждения'])
                    else:
                        normalized.update(group_id=None,name=municipality,community_count=number(r.get('Всего пабликов')),
                                          p31_ratio=number(r.get('Соответствие минимальным требованиям')))
                    normalized.update(source_sheet=sheet,source_row=rowno)
                    records.append(normalized)
            elif any(ALIASES.get(v,v) in ('p33','er','group_id') for v in headers.values() if v):
                # Generic user-authored workbook: canonical columns, explicit units %.
                if 'Регион' in headers.values():continue
                records.extend(_normalize({title:cells.get(col) for col,title in headers.items() if title})
                               for _,cells in rows[1:] if cells)
    else:raise ValueError('Only .csv or .xlsx imports supported')
    for r in records:
        if start:r['period_start']=start
        if end:r['period_end']=end
        if as_of:r['as_of_date']=as_of
        r.setdefault('measured_at',None)
        r.setdefault('period_status','unconfirmed')
    records=[r for r in records if not any(k in r and r[k] not in (None,'',municipality)
             for k in ('Муниципальное образование','municipality'))]
    if not records:raise ValueError('No matching official measurements')
    return records


def registry_from_official(rows):
    groups=[dict(id=r['group_id'],name=r.get('name',str(r['group_id'])),included=True,
                 business_card=False,business_card_confirmed=False,source='official_import')
            for r in rows if r.get('group_id')]
    if len({g['id'] for g in groups})!=len(groups):raise ValueError('Duplicate official community IDs require review')
    return groups
