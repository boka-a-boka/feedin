from datetime import datetime, timezone
from feedin import app, database
from feedin.models import Epoca, Selo, Usuario, ConquistaSelo


def popular_sistema():
    with app.app_context():
        print("⏳ Iniciando carga do Grafo Espaço-Temporal e Selos com a estrutura real...")

        # 1. POPULAR AS ÉPOCAS (Casando com nome_exibicao, rotulo_interno e eh_vigente)
        epocas_base = [
            {"exibicao": "Anos 50", "interno": "anos_50", "ordem": 1, "vigente": False},
            {"exibicao": "Anos 60", "interno": "anos_60", "ordem": 2, "vigente": False},
            {"exibicao": "Anos 70", "interno": "anos_70", "ordem": 3, "vigente": False},
            {"exibicao": "Anos 80", "interno": "anos_80", "ordem": 4, "vigente": False},
            {"exibicao": "Anos 90", "interno": "anos_90", "ordem": 5, "vigente": False},
            {"exibicao": "Anos 2000", "interno": "anos_2000", "ordem": 6, "vigente": False},
            {"exibicao": "Anos 2010", "interno": "anos_2010", "ordem": 7, "vigente": False},
            {"exibicao": "Anos 2020 (Atual)", "interno": "anos_2020", "ordem": 8, "vigente": True},
        ]

        for ep in epocas_base:
            # Usando a consulta explícita via session e filtrando pelo rótulo interno (chave única)
            existe = database.session.query(Epoca).filter(Epoca.rotulo_interno == ep["interno"]).first()
            if not existe:
                nova_epoca = Epoca(
                    nome_exibicao=ep["exibicao"],
                    rotulo_interno=ep["interno"],
                    ordem_cronologica=ep["ordem"],
                    eh_vigente=ep["vigente"]
                )
                database.session.add(nova_epoca)
                print(f"🔹 Época adicionada: {ep['exibicao']}")

        # 2. POPULAR O CATÁLOGO DE SELOS (Casando com nome, codigo_interno e icone_class)
        selos_base = [
            {
                "nome": "Pioneiro do FeedIn",
                "codigo": "pioneiro",
                "descricao": "Membro fundador que ajudou a iniciar a teia de confiança e o resgate da memória.",
                "icone": "bi-seedling"  # Ícone Bootstrap correspondente
            },
            {
                "nome": "Preservador da Memória",
                "codigo": "preservador_memoria",
                "descricao": "Concedido a quem resgata a história compartilhando fotos de antepassados e acervos antigos.",
                "icone": "bi-bank"  # Ícone Bootstrap correspondente (lembra museu/instituição)
            }
        ]

        for s in selos_base:
            existe = database.session.query(Selo).filter(Selo.codigo_interno == s["codigo"]).first()
            if not existe:
                novo_selo = Selo(
                    nome=s["nome"],
                    codigo_interno=s["codigo"],
                    descricao=s["descricao"],
                    icone_class=s["icone"],
                    exibir_no_card=True,
                    ativo=True
                )
                database.session.add(novo_selo)
                print(f"🏅 Selo catalogado: {s['nome']}")

        database.session.commit()

        # 3. MIGRAÇÃO DOS PIONEIROS ANTIGOS (Corrigido para 'is_pioneiro')
        selo_pioneiro = database.session.query(Selo).filter(Selo.codigo_interno == "pioneiro").first()

        if selo_pioneiro:
            # Buscando todos os usuários marcados como pioneiros
            usuarios_pioneiros = database.session.query(Usuario).filter(Usuario.is_pioneiro == True).all()

            for usuario in usuarios_pioneiros:
                ja_tem = database.session.query(ConquistaSelo).filter(
                    ConquistaSelo.id_usuario == usuario.id,
                    ConquistaSelo.id_selo == selo_pioneiro.id
                ).first()

                if not ja_tem:
                    # --- INTEGRAÇÃO DA FLAG DE AUDITORIA ---
                    # Como essa carga migra quem já era pioneiro nativo no Beta,
                    # marcamos como False (ganhou pelo processo do sistema).
                    # Se preferir considerar que a migração conta como ação de Admin, mude para True.
                    conquista = ConquistaSelo(
                        id_usuario=usuario.id,
                        id_selo=selo_pioneiro.id,
                        atribuido_por_admin=False
                    )
                    database.session.add(conquista)
                    print(f"🤝 Medalha de Pioneiro vinculada ao usuário: {usuario.username} (Auditoria: Sistema)")

            database.session.commit()

        print("🚀 Carga inicial executada com sucesso e base consistente!")


if __name__ == "__main__":
    popular_sistema()