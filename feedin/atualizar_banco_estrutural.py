# atualizar_banco_vps.py
import os
from feedin import app, database
from sqlalchemy import text

print("=" * 60)
print("🌐 SCRIPT DE ATUALIZAÇÃO DO BANCO DE DADOS - PRODUÇÃO (VPS)")
print("=" * 60)

with app.app_context():
    # -----------------------------------------------------------------
    # PASSO 1: Importação dos Modelos (Garante que o SQLAlchemy mapeie tudo)
    # -----------------------------------------------------------------
    print("📦 Mapeando modelos do sistema...")
    try:
        from feedin.models import Usuario, Postagem, Local, Taxonomia
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
    print("\n📝 Injetando campos temporais na tabela 'postagens' de produção...")

    # Sintaxe limpa e compatível com PostgreSQL, MySQL e SQLite
    alteracoes_postagem = [
        ("preservacao_memoria", "BOOLEAN DEFAULT FALSE"),
        ("id_epoca", "INTEGER REFERENCES epocas(id)"),
        ("id_empresa", "INTEGER REFERENCES ese_empresa(id)")  # Nome da tabela física da agenda
    ]

    for coluna, tipo in alteracoes_postagem:
        try:
            database.session.execute(text(f"ALTER TABLE postagens ADD COLUMN {coluna} {tipo};"))
            database.session.commit()
            print(f"   🔹 Coluna '{coluna}' injetada com sucesso.")
        except Exception as e_alter:
            database.session.rollback()
            msg_erro = str(e_alter).lower()
            if "duplicate" in msg_erro or "already exists" in msg_erro:
                print(f"   🔹 Coluna '{coluna}' já está presente na tabela.")
            else:
                print(f"   ⚠️ Nota/Aviso sobre a coluna '{coluna}': {e_alter}")

print("\n" + "=" * 60)
print("🏁 Banco de dados da VPS atualizado e protegido contra esquecimentos!")
print("=" * 60)