import os
from feedin import app, database
from sqlalchemy import text

print("=" * 60)
print("🌐 SCRIPT V2: INCREMENTAL - EXPANSÃO DE REPOST EM COMENTÁRIOS")
print("=" * 60)

with app.app_context():
    print("\n📝 Injetando campo de repost na tabela 'postagem_comentarios'...")
    try:
        # Resolve o erro operacional adicionando fisicamente o campo que o SQLAlchemy pediu
        database.session.execute(text("ALTER TABLE postagem_comentarios ADD COLUMN id_repost_original INTEGER;"))
        database.session.commit()
        print("   ✅ Coluna 'id_repost_original' injetada com sucesso!")

        # Amarração de integridade para o PostgreSQL da VPS
        try:
            database.session.execute(text(
                "ALTER TABLE postagem_comentarios ADD CONSTRAINT fk_comentarios_repost "
                "FOREIGN KEY (id_repost_original) REFERENCES postagens(id);"
            ))
            database.session.commit()
            print("   🔗 Restrição Foreign Key aplicada em 'postagem_comentarios'.")
        except Exception as e_fk:
            database.session.rollback()
            if "syntax error" in str(e_fk).lower() or "sqlite" in str(database.engine.url):
                pass
            else:
                print(f"   ⚠️ Nota sobre a FK nos comentários: {e_fk}")

    except Exception as e_com:
        database.session.rollback()
        msg_erro = str(e_com).lower()
        if "duplicate" in msg_erro or "already exists" in msg_erro:
            print("   🔹 Coluna 'id_repost_original' já está presente na tabela de comentários.")
        else:
            print(f"   ❌ Falha ao alterar a tabela de comentários: {e_com}")

print("\n" + "=" * 60)
print("🏁 Script V2 incremental concluído com sucesso!")
print("=" * 60)