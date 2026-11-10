import os
import sys
from datetime import datetime, timezone

# Adiciona o diretório atual ao path para garantir o reconhecimento do pacote
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from feedin import create_app
from feedin import database as db

app = create_app()

def ajustar_modulos_empresa_576():
    with app.app_context():
        # Importação local estrita dentro do app_context para evitar circular import
        from feedin.modules.empresa.models import ModEmpresaModulo

        print("🔍 [VPS AUDIT] Verificando registros da empresa 576 em mod_empresa_modulos...")
        
        # 1. Remove registros antigos da empresa 576 para evitar lixo ou duplicidades
        registros_antigos = ModEmpresaModulo.query.filter_by(empresa_id=576).all()
        for reg in registros_antigos:
            db.session.delete(reg)
        
        db.session.commit()
        print("🧹 [VPS AUDIT] Registros anteriores removidos com sucesso.")

        agora = datetime.now(timezone.utc)
        vencimento_pro = datetime(2026, 11, 30, 0, 33, 20, 85350, tzinfo=timezone.utc)

        # 2. Insere o Módulo Agenda (PRO Vitalício)
        agenda_mod = ModEmpresaModulo(
            empresa_id=576,
            modulo_slug='agenda',
            tipo_plano='pro_vitalicio',
            ativo=True,
            status_homologacao='homologado',
            limite_profissionais=999,
            limite_clientes=9999,
            data_vencimento=None,
            data_contratacao=agora
        )

        # 3. Insere o Módulo Empresa Pro (PRO com vencimento específico)
        empresa_mod = ModEmpresaModulo(
            empresa_id=576,
            modulo_slug='empresa_pro',
            tipo_plano='pro_vitalicio',
            ativo=True,
            status_homologacao='homologado',
            limite_profissionais=999,
            limite_clientes=999,
            data_vencimento=vencimento_pro,
            data_contratacao=agora
        )

        db.session.add(agenda_mod)
        db.session.add(empresa_mod)
        db.session.commit()

        print("✅ [VPS SUCCESS] Módulos da empresa 576 inseridos e configurados com sucesso!")

        # 4. Auditoria de confirmação imediata
        print("\n--- AUDITORIA FINAL NA VPS ---")
        registros_atuais = ModEmpresaModulo.query.filter_by(empresa_id=576).all()
        for r in registros_atuais:
            print(f"Empresa: {r.empresa_id} | Módulo: {r.modulo_slug} | Plano: {r.tipo_plano} | Limite Profissionais: {r.limite_profissionais} | Limite Clientes: {r.limite_clientes}")

if __name__ == '__main__':
    ajustar_modulos_empresa_576()
