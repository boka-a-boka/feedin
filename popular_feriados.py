import requests
from datetime import datetime
from feedin import database as db
from feedin.modules.empresa.models import CadastroFeriado  # Ajuste o import conforme sua estrutura


def popular_feriados_ano_corrente(ano=None):
    """
    Busca os feriados nacionais na BrasilAPI para o ano especificado
    (ou o ano atual se não informado) e popula o banco de dados de forma segura.
    """
    if ano is None:
        ano = datetime.now().year

    url = f"https://brasilapi.com.br/api/feriados/v1/{ano}"

    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            print(f"Erro ao acessar BrasilAPI (Status {response.status_code})")
            return False

        feriados_api = response.json()
        novos_registros = 0

        for f in feriados_api:
            # Converte a string 'YYYY-MM-DD' para objeto Date do Python
            data_formatada = datetime.strptime(f['date'], "%Y-%m-%d").date()

            # Evita duplicidade se o script for rodado mais de uma vez
            feriado_existe = CadastroFeriado.query.filter_by(data=data_formatada).first()

            if not feriado_existe:
                novo_feriado = CadastroFeriado(
                    nome=f['name'],
                    data=data_formatada,
                    abrangencia='nacional',
                    localidade='BR'
                )
                db.session.add(novo_feriado)
                novos_registros += 1

        if novos_registros > 0:
            db.session.commit()
            print(f"Sucesso! {novos_registros} feriados nacionais de {ano} foram inseridos no banco.")
        else:
            print(f"Calendário de {ano} já estava totalmente atualizado no banco.")

        return True

    except Exception as e:
        db.session.rollback()
        print(f"Falha crítica na automação de calendário: {str(e)}")
        return False


def limpar_feriados_antigos():
    """
    Remove feriados de anos anteriores para não acumular lixo eletrônico no banco.
    Opcional, mantendo a consistência do sistema leve.
    """
    ano_atual = datetime.now().year
    try:
        # Deleta tudo que for menor que 1º de Janeiro do ano corrente
        db.session.query(CadastroFeriado).filter(
            CadastroFeriado.data < datetime(ano_atual, 1, 1).date()
        ).delete()
        db.session.commit()
        print("Higienização concluída: Feriados antigos limpos com sucesso.")
    except Exception as e:
        db.session.rollback()
        print(f"Erro ao limpar banco: {str(e)}")