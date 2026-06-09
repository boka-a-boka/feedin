# verificar_banco.py
import os
from feedin import app, database

# Garante que estamos rodando dentro do contexto da aplicação Flask
with app.app_context():
    print("=" * 60)
    print("🔍 SISTEMA DE DIAGNÓSTICO DO BANCO DE DADOS - FEEDIN")
    print("=" * 60)

    # 1. Verificar conexão e caminho do arquivo do banco
    db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    print(f"📍 Banco de dados configurado em: {db_uri}")

    try:
        from feedin.models import Usuario, Postagem, Local, Taxonomia

        # --- TABELA USUARIO ---
        print("\n--- 👥 TABELA: Usuario ---")
        total_usuarios = Usuario.query.count()
        print(f"Total de registros: {total_usuarios}")
        if total_usuarios > 0:
            amostra_u = Usuario.query.first()
            print(f"Amostra - ID: {amostra_u.id} | Username: {amostra_u.username} | E-mail: {amostra_u.email}")
            print(f"Colunas disponíveis: {[c.name for c in Usuario.__table__.columns]}")

        # --- TABELA POSTAGEM ---
        print("\n--- 📝 TABELA: Postagem ---")
        try:
            total_postagens = Postagem.query.count()
            print(f"Total de registros: {total_postagens}")
            if total_postagens > 0:
                amostra_p = Postagem.query.first()
                # Verifica propriedades de imagem e conteúdo
                img_attr = "imagem_nome" if hasattr(amostra_p, "imagem_nome") else "imagem_url" if hasattr(amostra_p,
                                                                                                           "imagem_url") else "N/A"
                text_attr = "conteudo" if hasattr(amostra_p, "conteudo") else "legenda" if hasattr(amostra_p,
                                                                                                   "legenda") else "N/A"
                print(
                    f"Amostra - ID: {amostra_p.id} | Atributo Imagem ({img_attr}): {getattr(amostra_p, img_attr, 'Vazio')}")
                print(f"Amostra - Atributo Texto ({text_attr}): {getattr(amostra_p, text_attr, 'Vazio')[:50]}...")
                print(f"Colunas disponíveis: {[c.name for c in Postagem.__table__.columns]}")
        except Exception as e:
            print(f"⚠️ Erro ao consultar Postagem: {e}")

        # --- TABELA LOCAIS ---
        print("\n--- 📍 TABELA: Local (ou Locais) ---")
        try:
            total_locais = Local.query.count()
            print(f"Total de registros: {total_locais}")
            if total_locais > 0:
                amostra_l = Local.query.first()
                print(
                    f"Amostra - ID: {amostra_l.id} | Nome: {amostra_l.nome} | Bairro: {getattr(amostra_l, 'bairro', 'N/A')}")
                # Verifica se há o campo 'inserido_automaticamente' ou equivalente
                auto_attr = "inserido_automaticamente" if hasattr(amostra_l, "inserido_automaticamente") else "N/A"
                print(f"Atributo Automático ({auto_attr}): {getattr(amostra_l, auto_attr, 'Não encontrado')}")
                print(f"Colunas disponíveis: {[c.name for c in Local.__table__.columns]}")
        except Exception as e:
            print(f"⚠️ Erro ao consultar Local: {e}")

        # --- TABELA TAXONOMIA (Tags) ---
        print("\n--- 🏷️ TABELA: Taxonomia (Tags/Gostos) ---")
        try:
            total_taxonomia = Taxonomia.query.count()
            print(f"Total de registros: {total_taxonomia}")
            if total_taxonomia > 0:
                amostra_t = Taxonomia.query.first()
                print(
                    f"Amostra - ID: {amostra_t.id} | Nome: {amostra_t.nome} | Categoria: {getattr(amostra_t, 'categoria', 'N/A')}")
                print(f"Colunas disponíveis: {[c.name for c in Taxonomia.__table__.columns]}")
        except Exception as e:
            print(f"⚠️ Erro ao consultar Taxonomia: {e}")

    except ImportError as ie:
        print(f"\n❌ Erro crítico de importação de modelos: {ie}")
        print("Certifique-se de que os nomes dos modelos em 'from feedin.models import ...' estão corretos.")

    print("\n" + "=" * 60)