import xml.etree.ElementTree as ET
import calendar
import requests
import logging
from datetime import datetime, date
from flask import Flask, render_template, request, jsonify, make_response
from io import BytesIO

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'senado-chave-2025'

BASE    = "https://legis.senado.leg.br/dadosabertos"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/xml, text/xml, application/json, */*',
    'Accept-Language': 'pt-BR,pt;q=0.9',
}

# --------------------------------------------------------------------------
# SENADORES — XML (mais estável que JSON no Railway)
# --------------------------------------------------------------------------
def buscar_senadores_atuais():
    url = f"{BASE}/senador/lista/atual.xml"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        senadores = []
        for p in root.iter('Parlamentar'):
            ident = p.find('IdentificacaoParlamentar')
            if ident is None:
                continue
            cod  = ident.findtext('CodigoParlamentar', '')
            nome = ident.findtext('NomeParlamentar', '')
            part = ident.findtext('SiglaPartidoParlamentar', '')
            uf   = ident.findtext('UfParlamentar', '')
            foto = ident.findtext('UrlFotoParlamentar', '')
            if cod and nome:
                senadores.append({'codigo': cod, 'nome': nome,
                                   'partido': part, 'uf': uf, 'foto': foto})
        logger.info(f"Senadores: {len(senadores)}")
        return sorted(senadores, key=lambda x: x['nome'])
    except Exception as e:
        logger.error(f"Erro senadores: {e}")
        return []

# --------------------------------------------------------------------------
# CACHE por ano
# --------------------------------------------------------------------------
_cache = {}

def get_dados_ano(ano):
    if ano not in _cache:
        _cache[ano] = processar_dados_ano(ano)
    return _cache[ano]

# --------------------------------------------------------------------------
# PROCESSAMENTO: duas fontes de dados abertos
# --------------------------------------------------------------------------
def processar_dados_ano(ano):
    """
    FONTE 1 — ListaVotacoes{ano}.xml  (votações nominais)
      → sessoes_nominais: set de cod_sessao com votação nominal
      → votos_por_senador: { cod: {sessoes_nominal, sim, nao, abs, outros} }

    FONTE 2 — plenario/lista/votacao/{d1}/{d2}.json  (todas as votações)
      → sessoes_deliberacao: set de cod_sessao com qualquer votação
        (nominal OU simbólica) = TOTAL DE SESSÕES para presença
    """
    sessoes_deliberacao = set()
    sessoes_nominais    = set()
    votos_por_senador   = {}

    # ---- FONTE 1: XML nominais ----
    # Estrutura real (confirmada via /diagnostico):
    # <Votacao>
    #   <CodigoSessao>X</CodigoSessao>   <- direto na Votacao, SEM SessaoPlenaria
    #   <Votos><Voto>
    #     <CodigoParlamentar>Y</CodigoParlamentar>
    #     <Voto>Sim</Voto>               <- tag <Voto> dentro de <Voto>
    #   </Voto></Votos>
    # </Votacao>
    try:
        url = f"{BASE}/dados/ListaVotacoes{ano}.xml"
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.ok:
            root = ET.fromstring(r.content)
            for votacao in root.iter('Votacao'):
                # CodigoSessao direto na Votacao (não em SessaoPlenaria)
                cod_s = (votacao.findtext('CodigoSessao') or
                         votacao.findtext('NumeroSessao') or '').strip()
                if not cod_s:
                    continue
                sessoes_nominais.add(cod_s)
                sessoes_deliberacao.add(cod_s)

                # Votos em <Votos><Voto>
                votos_el = votacao.find('Votos')
                if votos_el is None:
                    continue
                for voto_el in votos_el.findall('Voto'):
                    cod_p = (voto_el.findtext('CodigoParlamentar') or '').strip()
                    if not cod_p:
                        continue
                    if cod_p not in votos_por_senador:
                        votos_por_senador[cod_p] = {
                            'sessoes_nominal': set(),
                            'sim': 0, 'nao': 0, 'abs': 0, 'outros': 0
                        }
                    votos_por_senador[cod_p]['sessoes_nominal'].add(cod_s)
                    # Valor do voto em <Voto> (tag homônima)
                    v = (voto_el.findtext('Voto') or '').lower()
                    if 'sim' in v:
                        votos_por_senador[cod_p]['sim'] += 1
                    elif 'não' in v or 'nao' in v:
                        votos_por_senador[cod_p]['nao'] += 1
                    elif 'abs' in v:
                        votos_por_senador[cod_p]['abs'] += 1
                    else:
                        votos_por_senador[cod_p]['outros'] += 1

            logger.info(f"XML {ano}: {len(sessoes_nominais)} sessões nominais, "
                        f"{len(votos_por_senador)} senadores")
        else:
            logger.warning(f"XML {ano} status {r.status_code}")
    except Exception as e:
        logger.error(f"Erro FONTE 1 {ano}: {e}")

    # ---- FONTE 2: endpoint votações por mês (nominal + simbólica) ----
    ano_atual = date.today().year
    mes_final = date.today().month if ano == ano_atual else 12

    for mes in range(2, mes_final + 1):
        d1 = f"{ano}{mes:02d}01"
        if mes == 12:
            d2 = f"{ano}1231"
        elif mes == date.today().month and ano == ano_atual:
            d2 = date.today().strftime('%Y%m%d')
        else:
            ultimo = calendar.monthrange(ano, mes)[1]
            d2 = f"{ano}{mes:02d}{ultimo:02d}"

        url_mes = f"{BASE}/plenario/lista/votacao/{d1}/{d2}.json"
        try:
            r = requests.get(url_mes,
                             headers={**HEADERS, 'Accept': 'application/json'},
                             timeout=15)
            if not r.ok:
                continue
            # Verifica se realmente voltou JSON
            ct = r.headers.get('Content-Type', '')
            if 'html' in ct:
                continue
            data = r.json()
            vots = (data.get("ListaVotacoes", {})
                        .get("Votacoes", {})
                        .get("Votacao", []))
            if isinstance(vots, dict):
                vots = [vots]
            for v in vots:
                # CodigoSessao direto no dict (mesma estrutura do XML)
                cod_s = (str(v.get("CodigoSessao", '') or '')
                         or str(v.get("NumeroSessao", '') or '')).strip()
                if cod_s and cod_s != '0' and cod_s != 'None':
                    sessoes_deliberacao.add(cod_s)
        except Exception as e:
            logger.warning(f"Erro FONTE 2 {mes}/{ano}: {e}")

    logger.info(f"Total {ano}: {len(sessoes_deliberacao)} sessões c/ deliberação, "
                f"{len(sessoes_nominais)} c/ nominal")
    return sessoes_deliberacao, sessoes_nominais, votos_por_senador

# --------------------------------------------------------------------------
# CÁLCULO DE PRESENÇA
# --------------------------------------------------------------------------
def calcular_presenca(codigo_senador, ano):
    """
    TOTAL DE SESSÕES  = sessões com qualquer votação (nominal ou simbólica)
    VOTAÇÕES NOMINAIS = sessões com nominal onde o senador votou
    PRESENTE          = sessões com nominal onde votou
    AUSENTE           = sessões com nominal onde não votou
    % PRESENÇA        = presente / total_com_nominal × 100
    """
    sessoes_delib, sessoes_nom, votos_por_sen = get_dados_ano(ano)
    dados = votos_por_sen.get(str(codigo_senador), {})
    sess_sen = dados.get('sessoes_nominal', set())

    total_sessoes   = len(sessoes_delib)
    total_nominais  = len(sessoes_nom)
    presente        = len(sess_sen)
    ausente         = len(sessoes_nom - sess_sen)
    pct             = round(presente / total_nominais * 100, 1) if total_nominais > 0 else 0
    total_votos     = sum(dados.get(k, 0) for k in ('sim','nao','abs','outros'))

    return {
        'total_sessoes':   total_sessoes,
        'total_nominais':  total_nominais,
        'presente':        presente,
        'ausente':         ausente,
        'pct_presenca':    pct,
        'total_votos':     total_votos,
        'votos': {k: dados.get(k,0) for k in ('sim','nao','abs','outros')},
    }

# --------------------------------------------------------------------------
# ROTAS
# --------------------------------------------------------------------------
@app.route('/diagnostico')
def diagnostico():
    """Testa conectividade com as fontes de dados do Senado."""
    import xml.etree.ElementTree as ET
    resultados = {}

    urls = {
        'xml_2025': f"{BASE}/dados/ListaVotacoes2025.xml",
        'xml_2026': f"{BASE}/dados/ListaVotacoes2026.xml",
        'senadores_xml': f"{BASE}/senador/lista/atual.xml",
        'votacoes_json': f"{BASE}/plenario/lista/votacao/20250201/20250228.json",
    }
    for nome, url in urls.items():
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            ct = r.headers.get('Content-Type', '')
            preview = r.text[:100] if r.text else ''
            resultados[nome] = {
                'status': r.status_code,
                'ok': r.ok,
                'content_type': ct,
                'preview': preview,
                'size': len(r.content)
            }
            # Se XML, tenta parsear e conta votações
            if r.ok and 'xml' in ct.lower():
                try:
                    root = ET.fromstring(r.content)
                    votacoes = list(root.iter('Votacao'))
                    sessoes = set()
                    for v in votacoes:
                        s = v.find('SessaoPlenaria')
                        if s is not None:
                            c = s.findtext('CodigoSessao') or s.findtext('NumeroSessao') or ''
                            if c: sessoes.add(c)
                    resultados[nome]['votacoes'] = len(votacoes)
                    resultados[nome]['sessoes_unicas'] = len(sessoes)
                    # Mostra tags da primeira votação
                    if votacoes:
                        v0 = votacoes[0]
                        resultados[nome]['tags_votacao'] = [c.tag for c in v0]
                        sp = v0.find('SessaoPlenaria')
                        if sp is not None:
                            resultados[nome]['tags_sessao'] = [c.tag for c in sp]
                        votos = list(v0.iter('VotoParlamentar'))
                        resultados[nome]['votos_1a_votacao'] = len(votos)
                        if votos:
                            resultados[nome]['tags_voto'] = [c.tag for c in votos[0]]
                except Exception as ex:
                    resultados[nome]['parse_error'] = str(ex)
        except Exception as e:
            resultados[nome] = {'erro': str(e)}

    return jsonify(resultados)

@app.route('/')
def index():
    ano_atual = datetime.now().year
    anos      = list(range(ano_atual, 2002, -1))
    senadores = buscar_senadores_atuais()
    return render_template('index.html', senadores=senadores,
                           anos=anos, ano_atual=ano_atual)

@app.route('/api/relatorio', methods=['POST'])
def api_relatorio():
    data    = request.get_json()
    codigos = data.get('codigos', [])
    ano     = int(data.get('ano', datetime.now().year))
    if not codigos:
        return jsonify({'error': 'Nenhum senador selecionado'}), 400

    resultado = []
    for cod in codigos:
        d = calcular_presenca(str(cod), ano)
        v = d['votos']
        resultado.append({
            'codigo': cod,
            'votacoes': {
                'total':     d['total_votos'],
                'sim':       v['sim'],
                'nao':       v['nao'],
                'abstencao': v['abs'],
                'outros':    v['outros'],
            },
            'presencas': {
                'total_sessoes':  d['total_sessoes'],
                'total_nominais': d['total_nominais'],
                'presente':       d['presente'],
                'ausente':        d['ausente'],
                'pct_presenca':   d['pct_presenca'],
            }
        })
    return jsonify({'ano': ano, 'resultado': resultado})

@app.route('/api/exportar_pdf', methods=['POST'])
def exportar_pdf():
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

    COR_VERDE       = colors.HexColor("#1A6B3A")
    COR_AZUL        = colors.HexColor("#0D2B5E")
    COR_CINZA       = colors.HexColor("#555555")
    COR_FUNDO       = colors.HexColor("#F5F5F5")
    COR_VERDE_CLARO = colors.HexColor("#E8F5EE")

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=1.8*cm, rightMargin=1.8*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    SS = getSampleStyleSheet()
    T  = ParagraphStyle("T", parent=SS["Title"],  fontSize=14, textColor=COR_VERDE,
                        alignment=TA_CENTER, leading=18)
    S  = ParagraphStyle("S", parent=SS["Normal"], fontSize=8.5, textColor=COR_CINZA, leading=12)
    B  = ParagraphStyle("B", parent=SS["Normal"], fontSize=10, fontName="Helvetica-Bold", leading=13)
    H  = ParagraphStyle("H", parent=SS["Normal"], fontSize=11, fontName="Helvetica-Bold",
                        textColor=COR_AZUL, leading=14)

    story = []
    story.append(Paragraph("Relatório de Presenças e Votações — Senado Federal", T))
    story.append(Paragraph(
        f"Ano: {ano}  |  Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M')}", S))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1.5, color=COR_VERDE))
    story.append(Spacer(1, 10))

    sen_map = {str(s['codigo']): s for s in senadores}

    for res in resultados:
        cod  = str(res['codigo'])
        sen  = sen_map.get(cod, {})
        v    = res.get('votacoes', {})
        p    = res.get('presencas', {})

        story.append(Paragraph(
            f"{sen.get('nome', cod)} ({sen.get('partido','')} / {sen.get('uf','')})", H))
        story.append(Spacer(1, 4))

        tabela = Table([
            [Paragraph("<b>Votações Nominais</b>", S), "",
             Paragraph("<b>Presenças em Plenário</b>", S), ""],
            [Paragraph("Total de votos", S),       Paragraph(str(v.get('total',0)), B),
             Paragraph("Sessões deliberativas", S), Paragraph(str(p.get('total_sessoes',0)), B)],
            [Paragraph("Votos SIM", S),             Paragraph(str(v.get('sim',0)), B),
             Paragraph("Sessões com nominal", S),   Paragraph(str(p.get('total_nominais',0)), B)],
            [Paragraph("Votos NÃO", S),             Paragraph(str(v.get('nao',0)), B),
             Paragraph("Presente (c/ nominal)", S), Paragraph(str(p.get('presente',0)), B)],
            [Paragraph("Abstenção", S),             Paragraph(str(v.get('abstencao',0)), B),
             Paragraph("Ausente (c/ nominal)", S),  Paragraph(str(p.get('ausente',0)), B)],
            [Paragraph("Outros", S),                Paragraph(str(v.get('outros',0)), B),
             Paragraph("% Presença", S),            Paragraph(f"{p.get('pct_presenca',0)}%", B)],
        ], colWidths=[4.5*cm, 2*cm, 5*cm, 2*cm])

        tabela.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,0), COR_VERDE_CLARO),
            ("SPAN",          (0,0),(1,0)),
            ("SPAN",          (2,0),(3,0)),
            ("GRID",          (0,0),(-1,-1), 0.3, colors.HexColor("#CCCCCC")),
            ("FONTSIZE",      (0,0),(-1,-1), 8.5),
            ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
            ("TOPPADDING",    (0,0),(-1,-1), 4),
            ("BOTTOMPADDING", (0,0),(-1,-1), 4),
            ("ROWBACKGROUNDS",(0,1),(-1,-1), [colors.white, COR_FUNDO]),
        ]))
        story.append(tabela)
        story.append(Spacer(1, 14))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                color=colors.HexColor("#CCCCCC")))
        story.append(Spacer(1, 10))

    doc.build(story)
    pdf = buf.getvalue(); buf.close()
    resp = make_response(pdf)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'attachment; filename="relatorio_senado_{ano}.pdf"'
    return resp

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=True)
