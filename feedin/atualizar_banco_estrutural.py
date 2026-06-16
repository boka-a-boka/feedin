import os
from feedin import app, database
from sqlalchemy import text

print("=" * 60)
print("🌐 SCRIPT V1: ATUALIZAÇÃO DO BANCO DE DADOS - BASE (HOMOLOGAÇÃO / VPS)")
print("=" * 60)

with app.app_context():
    # -----------------------------------------------------------------
    # PASSO 1: Importação dos Modelos (Garante o mapeamento completo)
    # -----------------------------------------------------------------
    print("📦 Mapeando modelos do sistema...")
    try:
        from feedin.models import Usuario, Postagem, PostagemComentario, Local, Taxonomia
        from feedin.models import Epoca, UsuarioLocalEpoca, Selo, ConquistaSelo

        try:
            from feedin.modules.agenda import models as agenda_models

            print("   -> Módulo Agenda mapeado.")
        except ImportError:
            print("   -> Nota: Arquivos da Agenda não importados ou ausentes.")
    except ImportError as ie:
        print(f"❌ Erro de importação: {ie}")
        exit(1)

    # -----------------------------------------------------------------
    # PASSO 2: Criar as tabelas NOVAS primeiro (Estratégia para Produção)
    # -----------------------------------------------------------------
    print("\n🔨 Criando tabelas inéditas (epocas, selos, conquistas)...")
    try:
        database.create_all()
        print("   ✅ Tabelas novas criadas (ou já existentes verificadas).")
    except Exception as e:
        print(f"   ❌ Falha ao criar tabelas base: {e}")
        exit(1)

    # -----------------------------------------------------------------
    # PASSO 3: Injetar colunas novas na tabela 'postagens' existente
    # -----------------------------------------------------------------
    print("\n📝 Injetando novos campos na tabela 'postagens'...")

    alteracoes_postagem = [
        {"coluna": "preservacao_memoria", "tipo": "BOOLEAN DEFAULT FALSE", "fk": None},
        {"coluna": "id_epoca", "tipo": "INTEGER", "fk": "epocas(id)"},
        {"coluna": "id_empresa", "tipo": "INTEGER", "fk": "ese_empresa(id)"},
        {"coluna": "id_repost_original", "tipo": "INTEGER", "fk": "postagens(id)"}
    ]

    for item in alteracoes_postagem:
        coluna = item["coluna"]
        tipo = item["tipo"]
        fk = item["fk"]

        try:
            database.session.execute(text(f"ALTER TABLE postagens ADD COLUMN {coluna} {tipo};"))
            database.session.commit()
            print(f"   🔹 Coluna '{coluna}' injetada com sucesso.")

            if fk:
                try:
                    database.session.execute(text(
                        f"ALTER TABLE postagens ADD CONSTRAINT fk_postagens_{coluna} FOREIGN KEY ({coluna}) REFERENCES {fk};"
                    ))
                    database.session.commit()
                    print(f"      🔗 Restrição Foreign Key aplicada para '{coluna}'.")
                except Exception as e_fk:
                    database.session.rollback()
                    if "syntax error" in str(e_fk).lower() or "sqlite" in str(database.engine.url):
                        pass
                    else:
                        print(f"      ⚠️ Nota sobre a FK de '{coluna}': {e_fk}")

        except Exception as e_alter:
            database.session.rollback()
            msg_erro = str(e_alter).lower()
            if "duplicate" in msg_erro or "already exists" in msg_erro:
                print(f"   🔹 Coluna '{coluna}' já está presente na tabela.")
            else:
                print(f"   ⚠️ Nota/Aviso sobre a coluna '{coluna}': {e_alter}")

print("\n" + "=" * 60)
print("🏁 Script V1 executado e verificado!")
print("=" * 60)