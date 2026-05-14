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

def buscar_presencas_senador(codigo_senador, ano=None):
    """
    O Senado não tem endpoint de presença direta.
    Usamos as votações nominais do ano para contar sessões em que o senador participou.
    Também buscamos o total de sessões plenárias do ano para calcular ausências.
    """
    # Busca votações do senador no ano para extrair sessões em que participou
    votacoes = buscar_votacoes_senador(codigo_senador, ano)

    # Conta sessões únicas em que o senador votou (=presente)
    sessoes_com_voto = set()
    for v in votacoes:
        sessao_id = v.get('codigo_sessao') or v.get('numero_sessao') or v.get('data')
        if sessao_id:
            sessoes_com_voto.add(str(sessao_id))

    # Busca total de sessões plenárias do ano via API de plenário
    total_sessoes = 0
    try:
        if ano:
            url = f"{BASE}/plenario/lista/votacao/{ano}.json"
            data = get_json(url)
            if data:
                votacoes_ano = (data
                    .get("ListaVotacoes", {})
                    .get("Votacoes", {})
                    .get("Votacao", []))
                if isinstance(votacoes_ano, dict):
                    votacoes_ano = [votacoes_ano]
                # Conta sessões únicas do ano
                sessoes_ano = set()
                for v in votacoes_ano:
                    s = v.get("SessaoPlenaria", {}) or {}
                    sid = s.get("CodigoSessao") or s.get("NumeroSessao")
                    if sid:
                        sessoes_ano.add(str(sid))
                total_sessoes = len(sessoes_ano)
    except Exception as e:
        logger.warning(f"Erro ao buscar total de sessões {ano}: {e}")
        total_sessoes = 0

    presente   = len(sessoes_com_voto)
    ausente    = max(0, total_sessoes - presente)

    return {
        'total':        total_sessoes,
        'presente':     presente,
        'ausente':      ausente,
        'justificado':  0,  # API não fornece esse dado separado
        'pct_presenca': round(presente / total_sessoes * 100, 1) if total_sessoes > 0 else 0
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
