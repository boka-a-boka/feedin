import os
import sys
import requests
from datetime import datetime, date, timedelta

# --- 1. CONFIGURAÇÃO ABSTRATA DO PATH (Previne qualquer erro de importação) ---
# Encontra a raiz absoluta (ProjetoFeedIn) independente de onde o script seja chamado
base_dir = os.path.abspath(os.path.dirname(__file__))  # empresa/services/
while os.path.basename(base_dir) != "ProjetoFeedIn" and len(base_dir) > 3:
    base_dir = os.path.dirname(base_dir)

if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

# --- 2. FUNÇÕES DO MOTOR ---

def obter_domingo_pascoa(ano: int) -> date:
    """Calcula matematicamente o domingo de Páscoa."""
    a = ano % 19
    b = ano // 100
    c = ano % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = ((h + l - 7 * m + 114) % 31) + 1
    return date(ano, mes, dia)


def obter_segundo_domingo_maio(ano: int) -> date:
    dt = date(ano, 5, 1)
    domingos = 0
    while domingos < 2:
        if dt.weekday() == 6:
            domingos += 1
            if domingos == 2:
                return dt
        dt += timedelta(days=1)
    return dt


def obter_segundo_domingo_agosto(ano: int) -> date:
    dt = date(ano, 8, 1)
    domingos = 0
    while domingos < 2:
        if dt.weekday() == 6:
            domingos += 1
            if domingos == 2:
                return dt
        dt += timedelta(days=1)
    return dt


def popular_feriados_fallback_nacionais(database, models_dict, ano: int):
    CadastroFeriado = models_dict['CadastroFeriado']
    pascoa = obter_domingo_pascoa(ano)

    feriados_fixos = [
        {"nome": "Confraternização Universal", "dia": 1, "mes": 1},
        {"nome": "Tiradentes", "dia": 21, "mes": 4},
        {"nome": "Dia do Trabalho", "dia": 1, "mes": 5},
        {"nome": "Independência do Brasil", "dia": 7, "mes": 9},
        {"nome": "Nossa Senhora Aparecida", "dia": 12, "mes": 10},
        {"nome": "Finados", "dia": 2, "mes": 11},
        {"nome": "Proclamação da República", "dia": 15, "mes": 11},
        {"nome": "Dia Nacional de Zumbi e da Consciência Negra", "dia": 20, "mes": 11},
        {"nome": "Natal", "dia": 25, "mes": 12},
    ]

    feriados_moveis = [
        {"nome": "Carnaval", "data": pascoa - timedelta(days=47)},
        {"nome": "Sexta-Feira Santa", "data": pascoa - timedelta(days=2)},
        {"nome": "Corpus Christi", "data": pascoa + timedelta(days=60)},
    ]

    for f in feriados_fixos:
        data_f = date(ano, f["mes"], f["dia"])
        if not database.session.query(CadastroFeriado).filter_by(data=data_f, localidade="BR").first():
            database.session.add(CadastroFeriado(nome=f["nome"], data=data_f, abrangencia="nacional", localidade="BR"))

    for f in feriados_moveis:
        if not database.session.query(CadastroFeriado).filter_by(data=f["data"], localidade="BR").first():
            database.session.add(CadastroFeriado(nome=f["nome"], data=f["data"], abrangencia="nacional", localidade="BR"))

    database.session.commit()


def popular_feriados_e_sazonais_locais(database, models_dict, ano: int, estado: str, cidade: str):
    CadastroFeriado = models_dict['CadastroFeriado']
    estado = estado.upper().strip()
    cidade_normalizada = cidade.lower().strip()

    if estado == "SP":
        data_constitucionalista = date(ano, 7, 9)
        if not database.session.query(CadastroFeriado).filter_by(data=data_constitucionalista, localidade="SP").first():
            database.session.add(CadastroFeriado(
                nome="Revolução Constitucionalista",
                data=data_constitucionalista,
                abrangencia="estadual",
                localidade="SP"
            ))

    if "piracicaba" in cidade_normalizada:
        data_pira = date(ano, 8, 1)
        if not database.session.query(CadastroFeriado).filter_by(data=data_pira, localidade=cidade).first():
            database.session.add(CadastroFeriado(
                nome="Aniversário de Piracicaba e Dia de Santo Estêvão",
                data=data_pira,
                abrangencia="municipal",
                localidade=cidade
            ))

    dia_das_maes = obter_segundo_domingo_maio(ano)
    dia_dos_pais = obter_segundo_domingo_agosto(ano)

    datas_comerciais = [
        {"nome": "Dia das Mães", "data": dia_das_maes},
        {"nome": "Dia dos Pais", "data": dia_dos_pais},
        {"nome": "Dia dos Namorados", "data": date(ano, 6, 12)},
        {"nome": "Dia de São Francisco de Assis (Padroeiro dos Animais/Pets)", "data": date(ano, 10, 4)},
    ]

    for dc in datas_comerciais:
        if not database.session.query(CadastroFeriado).filter_by(data=dc["data"], nome=dc["nome"]).first():
            database.session.add(CadastroFeriado(
                nome=dc["nome"],
                data=dc["data"],
                abrangencia="comercial",
                localidade="BR"
            ))

    database.session.commit()


def processar_localidade_completa(database, models_dict, local_id: int):
    Local = models_dict['Local']
    CadastroFeriado = models_dict['CadastroFeriado']

    estabelecimento = database.session.query(Local).get(local_id)
    if not_estabelecimento := (not estabelecimento):
        print(f"❌ [ERRO] Estabelecimento com ID {local_id} não encontrado.")
        return False

    cidade = estabelecimento.cidade
    estado = estabelecimento.estado

    if not cidade or not estado:
        print(f"⚠️  [ALERTA] Local ID {local_id} ({estabelecimento.nome}) está sem cidade/estado.")
        return False

    print(f"\n🌍 [MOTOR] Processando calendário para: {estabelecimento.nome} ({cidade} - {estado})")

    for ano in [2026, 2027]:
        print(f"   📅 Sincronizando ano {ano}...")
        sucesso_api = False
        url_nacionais = f"https://brasilapi.com.br/api/feriados/v1/{ano}"

        try:
            resposta = requests.get(url_nacionais, timeout=6)
            if resposta.status_code == 200:
                feriados = resposta.json()
                for f in feriados:
                    data_formatada = datetime.strptime(f["date"], "%Y-%m-%d").date()
                    if not database.session.query(CadastroFeriado).filter_by(data=data_formatada, localidade="BR").first():
                        database.session.add(CadastroFeriado(
                            nome=f["name"],
                            data=data_formatada,
                            abrangencia="nacional",
                            localidade="BR"
                        ))
                database.session.commit()
                sucesso_api = True
                print(f"      ✅ Feriados Nacionais via API populados para {ano}.")
        except Exception as e:
            print(f"      ⚠️  Falha ao conectar na API ({e}). Usando fallback matemático...")
            database.session.rollback()

        if not sucesso_api:
            popular_feriados_fallback_nacionais(database, models_dict, ano)
            print(f"      🧩 Fallback matemático nacional gerado para {ano}.")

        popular_feriados_e_sazonais_locais(database, models_dict, ano, estado, cidade)
        print(f"      🚀 Particularidades de {cidade}/{estado} e datas sazonais injetadas.")

    print(f"🏁 [SUCESSO] Sincronização de calendário concluída para o Local {local_id}!\n")
    return True


# Atalho global para manter a compatibilidade com rotas antigas
popular_feriados_civis_dinamico = processar_localidade_completa