from flask import Flask, render_template, request, jsonify, make_response
import requests
import json
import logging
from datetime import datetime
from functools import lru_cache
import re
import xml.etree.ElementTree as ET

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'senado-chave-2025'

BASE = "https://legis.senado.leg.br/dadosabertos"
HEADERS = {"Accept": "application/json"}

# --------------------------------------------------------------------------
# HELPERS DE API
# --------------------------------------------------------------------------
def get_json(url, params=None, timeout=15):
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Erro GET {url}: {e}")
        return None

def get_xml(url, timeout=15):
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return ET.fromstring(r.content)
    except Exception as e:
        logger.warning(f"Erro GET XML {url}: {e}")
        return None

# --------------------------------------------------------------------------
# SENADORES
# --------------------------------------------------------------------------
def buscar_senadores_atuais():
    """Lista todos os senadores em exercício."""
    url = f"{BASE}/senador/lista/atual.json"
    data = get_json(url)
    if not data:
        return []
    try:
        parlamentares = (data
            .get("ListaParlamentarEmExercicio", {})
            .get("Parlamentares", {})
            .get("Parlamentar", []))
        senadores = []
        for p in parlamentares:
            ident = p.get("IdentificacaoParlamentar", {})
            senadores.append({
                "codigo":   ident.get("CodigoParlamentar", ""),
                "nome":     ident.get("NomeParlamentar", ""),
                "nome_completo": ident.get("NomeCompletoParlamentar", ""),
                "partido":  ident.get("SiglaPartidoParlamentar", ""),
                "uf":       ident.get("UfParlamentar", ""),
                "foto":     ident.get("UrlFotoParlamentar", ""),
                "email":    ident.get("EmailParlamentar", ""),
            })
        return sorted(senadores, key=lambda x: x["nome"])
    except Exception as e:
        logger.error(f"Erro ao parsear senadores: {e}")
        return []

def buscar_votacoes_senador(codigo_senador, ano=None):
    """Busca votações nominais de um senador."""
    url = f"{BASE}/senador/{codigo_senador}/votacoes.json"
    params = {}
    if ano:
        params["ano"] = ano
    data = get_json(url, params=params)
    if not data:
        return []
    try:
        votacoes_raw = (data
            .get("VotacaoParlamentar", {})
            .get("Parlamentar", {})
            .get("Votacoes", {})
            .get("Votacao", []))
        if isinstance(votacoes_raw, dict):
            votacoes_raw = [votacoes_raw]
        votacoes = []
        for v in votacoes_raw:
            materia = v.get("IdentificacaoMateria", {}) or {}
            sessao  = v.get("SessaoPlenaria", {}) or {}
            votacoes.append({
                "data":         sessao.get("DataSessao", ""),
                "hora":         sessao.get("HoraInicio", ""),
                "numero_sessao": sessao.get("NumeroSessao", ""),
                "tipo_sessao":  sessao.get("SiglaTipoSessao", ""),
                "codigo_sessao": sessao.get("CodigoSessao", ""),
                "materia":      f"{materia.get('SiglaSubtipoMateria','')}/{materia.get('NumeroMateria','')}/{materia.get('AnoMateria','')}",
                "voto":         v.get("DescricaoVoto", ""),
                "resultado":    v.get("DescricaoResultado", ""),
                "secreta":      v.get("IndicadorVotacaoSecreta", "N") == "S",
            })
        return votacoes
    except Exception as e:
        logger.error(f"Erro ao parsear votações de {codigo_senador}: {e}")
        return []

HEADERS_SCRAPER = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9',
    'Connection': 'keep-alive',
}

def scrape_sessoes_do_ano(ano):
    """
    Scrapa a lista de sessões deliberativas do ano navegando
    pela página de sessão plenária do Senado.
    Retorna lista de dicts: {id_sessao, numero, data, tipo, url}
    """
    sessoes = []
    # A página de sessão plenária carrega via JS, mas podemos usar
    # o endpoint de agenda que retorna HTML com as sessões por mês
    try:
        from datetime import date
        ano_atual = date.today().year
        mes_final = 12 if ano < ano_atual else date.today().month

        for mes in range(2, mes_final + 1):  # Fev a Dez (recesso Jan)
            data_str = f"{ano}{mes:02d}01"
            # Endpoint que retorna sessões do mês como JSON via portlet
            url = f"https://legis.senado.leg.br/dadosabertos/plenario/legislatura/sessaoLegislativa/{data_str}.json"
            r = requests.get(url, headers=HEADERS_SCRAPER, timeout=10)
            if r.ok:
                try:
                    data = r.json()
                    sess_raw = (data.get("SessaoLegislativa", {})
                                    .get("SessoesPlenarias", {})
                                    .get("SessaoPlenaria", []))
                    if isinstance(sess_raw, dict):
                        sess_raw = [sess_raw]
                    for s in sess_raw:
                        if str(ano) in str(s.get("DataSessao", "")):
                            tipo = s.get("SiglaTipoSessao", "")
                            # Só sessões deliberativas contam para presença
                            if tipo in ["DO", "DE", "DOR", "DEX"]:
                                sessoes.append({
                                    "id": s.get("CodigoSessao", ""),
                                    "numero": s.get("NumeroSessao", ""),
                                    "data": s.get("DataSessao", ""),
                                    "tipo": tipo,
                                    "url": f"https://www25.senado.leg.br/web/atividade/sessao-plenaria/-/pauta/{s.get('CodigoSessao', '')}"
                                })
                except Exception:
                    pass
        logger.info(f"Sessões deliberativas {ano} via API: {len(sessoes)}")
    except Exception as e:
        logger.warning(f"Erro ao buscar sessões do ano: {e}")

    return sessoes

def scrape_presentes_sessao(id_sessao):
    """
    Scrapa a página HTML de uma sessão plenária e extrai
    os nomes dos senadores presentes (que usaram a palavra ou votaram).
    Retorna set de nomes normalizados.
    """
    url = f"https://www25.senado.leg.br/web/atividade/sessao-plenaria/-/pauta/{id_sessao}"
    presentes = set()
    try:
        r = requests.get(url, headers=HEADERS_SCRAPER, timeout=15)
        if not r.ok:
            return presentes

        from bs4 import BeautifulSoup
        import re

        soup = BeautifulSoup(r.text, 'html.parser')

        # Extrai todos os links de senadores no andamento da sessão
        # Padrão: "Senador(a) Nome (Partido/UF): Uso da Palavra"
        # ou links para perfil do senador
        texto_completo = soup.get_text()

        # Padrão de presença: nome do senador mencionado na sessão
        padroes = [
            r'Senator[ao]s?\s+([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇÀÈÌÒÙ][A-Za-záéíóúâêîôûãõçàèìòùÁÉÍÓÚÂÊÎÔÛÃÕÇÀÈÌÒÙ\s]+?)\s*\(',
            r'Senadora\s+([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇÀÈÌÒÙ][A-Za-záéíóúâêîôûãõçàèìòùÁÉÍÓÚÂÊÎÔÛÃÕÇÀÈÌÒÙ\s]+?)\s*\(',
        ]

        for li in soup.find_all('li'):
            texto = li.get_text()
            for padrao in padroes:
                matches = re.findall(padrao, texto)
                for m in matches:
                    nome = m.strip().upper()
                    if len(nome) > 3:
                        presentes.add(nome)

        # Também captura links diretos para perfil de senador
        for a in soup.find_all('a', href=re.compile(r'/senador/-/perfil/')):
            texto_link = a.get_text(strip=True).upper()
            # Remove prefixo "SENADOR(A)"
            for prefix in ['SENADOR ', 'SENADORA ', 'SENATOR ']:
                if texto_link.startswith(prefix):
                    texto_link = texto_link[len(prefix):]
            # Remove sufixo com partido/UF
            texto_link = re.sub(r'\s*\([^)]+\)\s*$', '', texto_link).strip()
            if len(texto_link) > 3:
                presentes.add(texto_link)

        logger.info(f"Sessão {id_sessao}: {len(presentes)} senadores identificados")

    except Exception as e:
        logger.warning(f"Erro ao scrape sessão {id_sessao}: {e}")

    return presentes

def normalizar_nome(nome):
    """Normaliza nome para comparação."""
    import unicodedata
    nome = nome.upper().strip()
    nome = unicodedata.normalize('NFKD', nome)
    nome = ''.join(c for c in nome if not unicodedata.combining(c))
    return nome

def buscar_presencas_senador(codigo_senador, ano=None):
    """
    Calcula presença do senador via:
    1. Scraping HTML das sessões plenárias do ano
    2. Cruzamento com o nome do senador
    3. Complemento com votações nominais da API
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Busca dados do senador para obter o nome
    url_sen = f"{BASE}/senador/{codigo_senador}.json"
    nome_senador = ""
    try:
        data = get_json(url_sen)
        if data:
            ident = (data.get("DetalheParlamentar", {})
                        .get("Parlamentar", {})
                        .get("IdentificacaoParlamentar", {}))
            nome_senador = normalizar_nome(ident.get("NomeParlamentar", ""))
    except Exception:
        pass

    # Fallback: busca votações nominais (mais rápido e confiável)
    votacoes = buscar_votacoes_senador(codigo_senador, ano)
    sessoes_votacao = set()
    for v in votacoes:
        cod = v.get('codigo_sessao') or v.get('numero_sessao')
        if cod:
            sessoes_votacao.add(str(cod).strip())

    # Busca pronunciamentos
    sessoes_disc = buscar_pronunciamentos_senador(codigo_senador, ano)

    # Busca sessões do ano para scraping
    sessoes_ano = scrape_sessoes_do_ano(ano) if ano else []
    total_sessoes = len(sessoes_ano)

    # Para cada sessão, verifica presença via scraping HTML
    sessoes_presentes_scrape = set()
    if nome_senador and sessoes_ano:
        def verificar_sessao(sessao):
            presentes = scrape_presentes_sessao(sessao['id'])
            for p in presentes:
                p_norm = normalizar_nome(p)
                # Verifica se alguma palavra do nome do senador está na lista
                partes_nome = [w for w in nome_senador.split() if len(w) > 3]
                if any(parte in p_norm for parte in partes_nome):
                    return sessao['id']
            return None

        # Processa em paralelo (máx 5 simultâneos para não sobrecarregar)
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(verificar_sessao, s): s for s in sessoes_ano}
            for future in as_completed(futures):
                resultado = future.result()
                if resultado:
                    sessoes_presentes_scrape.add(str(resultado))

    # Une todas as fontes de presença
    todas_presencas = sessoes_votacao | sessoes_disc | sessoes_presentes_scrape

    presente = len(todas_presencas)
    ausente  = max(0, total_sessoes - presente) if total_sessoes > 0 else 0
    pct      = round(presente / total_sessoes * 100, 1) if total_sessoes > 0 else 0

    return {
        'total':             total_sessoes,
        'presente':          presente,
        'sessoes_voto':      len(sessoes_votacao),
        'sessoes_disc':      len(sessoes_disc),
        'sessoes_scrape':    len(sessoes_presentes_scrape),
        'ausente':           ausente,
        'justificado':       0,
        'pct_presenca':      pct,
        'nota': 'Presença via votações nominais + pronunciamentos + sessões plenárias'
    }
    """Busca pronunciamentos do senador — indica presença na sessão."""
    url = f"{BASE}/senador/{codigo_senador}/discursos.json"
    params = {'casa': 'SF'}
    if ano:
        params['dataInicio'] = f"{ano}0101"
        params['dataFim']    = f"{ano}1231"
    data = get_json(url, params=params)
    if not data:
        return set()
    try:
        disc_raw = (data
            .get("DiscursosParlamentar", {})
            .get("Parlamentar", {})
            .get("Pronunciamentos", {})
            .get("Pronunciamento", []))
        if isinstance(disc_raw, dict):
            disc_raw = [disc_raw]
        sessoes = set()
        for d in disc_raw:
            sessao = d.get("SessaoPlenaria", {}) or {}
            cod = sessao.get("CodigoSessao") or sessao.get("NumeroSessao")
            if cod:
                sessoes.add(str(cod).strip())
        return sessoes
    except Exception as e:
        logger.warning(f"Erro ao parsear pronunciamentos de {codigo_senador}: {e}")
        return set()

def buscar_sessoes_do_ano(ano):
    """Busca todas as sessões com votação nominal do ano via XML oficial do Senado."""
    sessoes = set()
    try:
        url = f"https://legis.senado.leg.br/dadosabertos/dados/ListaVotacoes{ano}.xml"
        r = requests.get(url, timeout=20)
        if not r.ok:
            return sessoes
        root = ET.fromstring(r.content)
        for votacao in root.iter('Votacao'):
            sessao = votacao.find('SessaoPlenaria')
            if sessao is not None:
                cod = sessao.findtext('CodigoSessao') or sessao.findtext('NumeroSessao')
                if cod:
                    sessoes.add(cod.strip())
        logger.info(f"Sessões com votação nominal em {ano}: {len(sessoes)}")
    except Exception as e:
        logger.warning(f"Erro ao buscar sessões do ano {ano}: {e}")
    return sessoes

def buscar_presencas_senador(codigo_senador, ano=None):
    """
    Calcula presença unindo:
    - Sessões em que o senador VOTOU nominalmente
    - Sessões em que o senador fez PRONUNCIAMENTO
    Total = sessões únicas onde esteve presente por qualquer motivo.
    """
    votacoes = buscar_votacoes_senador(codigo_senador, ano)

    # Sessões com votação
    sessoes_votacao = set()
    for v in votacoes:
        cod = v.get('codigo_sessao') or v.get('numero_sessao')
        if cod:
            sessoes_votacao.add(str(cod).strip())

    # Sessões com pronunciamento
    sessoes_disc = buscar_pronunciamentos_senador(codigo_senador, ano)

    # União: presente em qualquer das duas
    sessoes_presente = sessoes_votacao | sessoes_disc

    # Total de sessões com votação nominal no ano (denominador)
    todas_sessoes = buscar_sessoes_do_ano(ano) if ano else set()

    total    = len(todas_sessoes) if todas_sessoes else len(sessoes_presente)
    presente = len(sessoes_presente)
    ausente  = max(0, len(todas_sessoes) - presente) if todas_sessoes else 0
    pct      = round(presente / total * 100, 1) if total > 0 else 0

    return {
        'total':          total,
        'presente':       presente,
        'sessoes_voto':   len(sessoes_votacao),
        'sessoes_disc':   len(sessoes_disc),
        'ausente':        ausente,
        'justificado':    0,
        'pct_presenca':   pct,
        'nota':           'Presença calculada por votações nominais + pronunciamentos'
    }

# --------------------------------------------------------------------------
# ROTAS
# --------------------------------------------------------------------------
@app.route('/')
def index():
    ano_atual = datetime.now().year
    anos = list(range(ano_atual, 2002, -1))
    senadores = buscar_senadores_atuais()
    return render_template('index.html', senadores=senadores, anos=anos, ano_atual=ano_atual)

@app.route('/api/senadores')
def api_senadores():
    senadores = buscar_senadores_atuais()
    return jsonify(senadores)

@app.route('/api/relatorio', methods=['POST'])
def api_relatorio():
    data = request.get_json()
    codigos   = data.get('codigos', [])
    ano       = data.get('ano', datetime.now().year)

    if not codigos:
        return jsonify({'error': 'Nenhum senador selecionado'}), 400

    resultado = []
    for cod in codigos:
        votacoes  = buscar_votacoes_senador(cod, ano)
        presencas = buscar_presencas_senador(cod, ano)

        total_votacoes = len(votacoes)
        votos_sim      = sum(1 for v in votacoes if 'sim' in v['voto'].lower())
        votos_nao      = sum(1 for v in votacoes if 'não' in v['voto'].lower() or 'nao' in v['voto'].lower())
        votos_abs      = sum(1 for v in votacoes if 'abs' in v['voto'].lower())
        votos_outros   = total_votacoes - votos_sim - votos_nao - votos_abs

        resultado.append({
            'codigo': cod,
            'votacoes': {
                'total':     total_votacoes,
                'sim':       votos_sim,
                'nao':       votos_nao,
                'abstencao': votos_abs,
                'outros':    votos_outros,
                'detalhes':  votacoes[:50]
            },
            'presencas': presencas  # já é um dict com total, presente, ausente, pct_presenca
        })

    return jsonify({'ano': ano, 'resultado': resultado})

@app.route('/api/exportar_pdf', methods=['POST'])
def exportar_pdf():
    """Gera PDF do relatório de presença e votações."""
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                     Table, TableStyle, HRFlowable)

    data       = request.get_json()
    senadores  = data.get('senadores', [])
    resultados = data.get('resultados', [])
    ano        = data.get('ano', datetime.now().year)

    COR_VERDE = colors.HexColor("#1A6B3A")
    COR_AZUL  = colors.HexColor("#0D2B5E")
    COR_CINZA = colors.HexColor("#555555")
    COR_FUNDO = colors.HexColor("#F5F5F5")
    COR_VERDE_CLARO = colors.HexColor("#E8F5EE")

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=1.8*cm, rightMargin=1.8*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    SS  = getSampleStyleSheet()
    T   = ParagraphStyle("T",  parent=SS["Title"],  fontSize=14, textColor=COR_VERDE, alignment=TA_CENTER, leading=18)
    S   = ParagraphStyle("S",  parent=SS["Normal"], fontSize=8.5, textColor=COR_CINZA, leading=12)
    B   = ParagraphStyle("B",  parent=SS["Normal"], fontSize=10, fontName="Helvetica-Bold", leading=13)
    H   = ParagraphStyle("H",  parent=SS["Normal"], fontSize=11, fontName="Helvetica-Bold", textColor=COR_AZUL, leading=14)

    story = []
    story.append(Paragraph("Relatório de Presenças e Votações — Senado Federal", T))
    story.append(Paragraph(f"Ano: {ano}  |  Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M')}", S))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1.5, color=COR_VERDE))
    story.append(Spacer(1, 10))

    # Mapa de senadores por código
    sen_map = {str(s['codigo']): s for s in senadores}

    for res in resultados:
        cod  = str(res['codigo'])
        sen  = sen_map.get(cod, {})
        nome = sen.get('nome', f'Senador {cod}')
        partido = sen.get('partido', '')
        uf      = sen.get('uf', '')

        story.append(Paragraph(f"{nome} ({partido}/{uf})", H))
        story.append(Spacer(1, 4))

        v = res.get('votacoes', {})
        p = res.get('presencas', {})

        tabela = Table([
            [Paragraph("<b>Votações Nominais</b>", S), "", Paragraph("<b>Presenças em Plenário</b>", S), ""],
            [Paragraph("Total", S), Paragraph(str(v.get('total',0)), B),
             Paragraph("Total de Sessões", S), Paragraph(str(p.get('total',0)), B)],
            [Paragraph("Votos SIM", S), Paragraph(str(v.get('sim',0)), B),
             Paragraph("Presente", S), Paragraph(str(p.get('presente',0)), B)],
            [Paragraph("Votos NÃO", S), Paragraph(str(v.get('nao',0)), B),
             Paragraph("Ausente", S), Paragraph(str(p.get('ausente',0)), B)],
            [Paragraph("Abstenção", S), Paragraph(str(v.get('abstencao',0)), B),
             Paragraph("Justificado", S), Paragraph(str(p.get('justificado',0)), B)],
            [Paragraph("Outros", S), Paragraph(str(v.get('outros',0)), B),
             Paragraph("% Presença", S), Paragraph(f"{p.get('pct_presenca',0)}%", B)],
        ], colWidths=[4*cm, 2*cm, 5*cm, 2.5*cm])

        tabela.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), COR_VERDE_CLARO),
            ("SPAN",       (0,0), (1,0)),
            ("SPAN",       (2,0), (3,0)),
            ("GRID",       (0,0), (-1,-1), 0.3, colors.HexColor("#CCCCCC")),
            ("FONTSIZE",   (0,0), (-1,-1), 8.5),
            ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING",(0,0),(-1,-1),4),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, COR_FUNDO]),
        ]))
        story.append(tabela)
        story.append(Spacer(1, 12))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCCCCC")))
        story.append(Spacer(1, 10))

    doc.build(story)
    pdf = buf.getvalue(); buf.close()

    resp = make_response(pdf)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'attachment; filename="relatorio_senado_{ano}.pdf"'
    return resp

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=True)
