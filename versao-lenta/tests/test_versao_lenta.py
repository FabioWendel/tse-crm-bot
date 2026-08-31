import os
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, call, patch

import crm_tse_bot as bot
import chrome_normal


PESSOA = bot.Pessoa(0, "PESSOA TESTE", "00000000000", "MAE TESTE", "01/01/2000")


class Relogio:
    def __init__(self):
        self.agora = 0.0

    def esperar(self, ms):
        self.agora += ms / 1000


class VersaoLentaTests(unittest.TestCase):
    def test_pausa_respeita_prazo_e_usa_blocos_curtos(self):
        relogio = Relogio()
        page = MagicMock()
        page.wait_for_timeout.side_effect = relogio.esperar
        with patch.object(bot.time, "monotonic", side_effect=lambda: relogio.agora):
            self.assertTrue(bot.aguardar_pausa(page, 2500, 5))
            self.assertEqual(relogio.agora, 2.5)
            self.assertEqual([c.args[0] for c in page.wait_for_timeout.call_args_list], [1000, 1000, 500])
            page.wait_for_timeout.reset_mock()
            self.assertFalse(bot.aguardar_pausa(page, 3000, 5))
            page.wait_for_timeout.assert_not_called()

    def test_perfis_persistentes_e_opcao_temporaria(self):
        pw = MagicMock()
        with patch.object(bot, "find_chrome_executable", return_value="chrome"), patch.object(
            bot, "find_tse_executable", return_value="chrome"
        ), patch.object(
            bot, "abrir_chrome_normal"
        ) as externo:
            bot.abrir_contexto_navegador(pw, tse=False)
            self.assertEqual(pw.chromium.launch_persistent_context.call_args.args, (str(bot.PROFILE_DIR),))
            pw.chromium.launch_persistent_context.reset_mock()
            bot.abrir_contexto_navegador(pw, tse=True)
            externo.assert_called_once_with(
                pw, executable_path="chrome", profile_dir=bot.TSE_PROFILE_DIR,
                clean=False, slow_mo=bot.TSE_SLOW_MO_MS, headless=False,
                port=bot.TSE_REMOTE_DEBUGGING_PORT,
            )
            pw.chromium.launch_persistent_context.assert_not_called()
            with patch.object(bot, "TSE_PERFIL_LIMPO", True), patch.object(bot, "CRM_PERFIL_LIMPO", True):
                bot.abrir_contexto_navegador(pw, tse=False)
                self.assertEqual(pw.chromium.launch_persistent_context.call_args.args, ("",))
                bot.abrir_contexto_navegador(pw, tse=True)
                self.assertTrue(externo.call_args.kwargs["clean"])

    def test_selecao_edge_localiza_executavel_instalado(self):
        with patch.object(bot, "TSE_NAVEGADOR", "edge"), patch.object(
            bot.sys, "platform", "win32"
        ), patch.object(Path, "is_file", return_value=True), patch.object(
            bot, "find_chrome_executable"
        ) as chrome:
            escolhido = bot.find_tse_executable()
            self.assertEqual(Path(escolhido).name, "msedge.exe")
            chrome.assert_not_called()

    def test_edge_ausente_nao_troca_para_chrome_silenciosamente(self):
        with patch.object(bot, "TSE_NAVEGADOR", "edge"), patch.object(
            bot.sys, "platform", "win32"
        ), patch.object(Path, "is_file", return_value=False), patch.object(
            bot, "find_chrome_executable"
        ) as chrome:
            with self.assertRaisesRegex(RuntimeError, "Edge nao encontrado"):
                bot.find_tse_executable()
            chrome.assert_not_called()

    def test_navegador_nao_suportado_falha_explicitamente(self):
        with patch.object(bot, "TSE_NAVEGADOR", "inexistente"):
            with self.assertRaisesRegex(RuntimeError, "TSE_NAVEGADOR"):
                bot.find_tse_executable()

    def test_contexto_tse_fica_aberto_ate_terminar_e_fecha_uma_vez(self):
        context = MagicMock()
        resultado = bot.resultado_sem_identificacao("ERRO", "teste")
        with patch.object(bot, "abrir_contexto_navegador", return_value=context), patch.object(
            bot, "consultar_tse_playwright_page", return_value=resultado
        ):
            recebido = bot.consultar_tse_chrome_normal(MagicMock(), PESSOA)
            context.close.assert_not_called()
            bot.fechar_tse_resultado(recebido)
            bot.fechar_tse_resultado(recebido)
            context.close.assert_called_once()

    def test_contexto_tse_compartilhado_nao_fecha_entre_consultas(self):
        context = MagicMock()
        page = MagicMock()
        page.is_closed.return_value = False
        context.pages = [page]
        resultado = bot.resultado_sem_identificacao("ERRO", "teste")
        with patch.object(bot, "consultar_tse_playwright_page", return_value=resultado):
            recebido = bot.consultar_tse_chrome_normal(MagicMock(), PESSOA, context)
        self.assertIs(recebido, resultado)
        self.assertIsNone(recebido.fechar_tse)
        context.close.assert_not_called()
        bot.fechar_tse_resultado(recebido)
        context.close.assert_not_called()

    def test_volta_tse_para_home_na_mesma_pagina(self):
        context = MagicMock()
        page = MagicMock()
        page.is_closed.return_value = False
        context.pages = [page]
        with patch.object(bot, "sair_do_perfil_tse", return_value=True) as sair, patch.object(
            bot, "ir_para", return_value=True
        ) as navegar, patch.object(
            bot, "wait_tse_loading"
        ) as carregar, patch.object(
            bot, "desautenticar_eleitor_tse", return_value=True
        ) as desautenticar:
            bot.voltar_tse_para_home(context)
        sair.assert_called_once_with(page)
        navegar.assert_called_once_with(page, bot.TSE_URL, tentativas=1)
        carregar.assert_called_once_with(page)
        desautenticar.assert_called_once_with(page)
        context.new_page.assert_not_called()

    def test_sai_pelo_botao_do_modal_antes_da_proxima_consulta(self):
        page = MagicMock()
        dialogo = MagicMock()
        botao_modal = MagicMock()
        botao_modal.is_visible.return_value = True
        dialogo.get_by_role.return_value.last = botao_modal
        page.get_by_role.return_value = dialogo
        with patch.object(bot, "wait_tse_loading") as carregar:
            self.assertTrue(bot.sair_do_perfil_tse(page))
        dialogo.get_by_role.assert_called_once()
        botao_modal.scroll_into_view_if_needed.assert_called_once_with(timeout=5000)
        botao_modal.click.assert_called_once_with(timeout=8000)
        self.assertEqual(carregar.call_count, 2)

    def test_desautentica_eleitor_pelo_link_nao_sou_este_eleitor(self):
        page = MagicMock()
        link = MagicMock()
        link.is_visible.return_value = True
        page.get_by_role.return_value.first = link
        with patch.object(bot, "wait_tse_loading"), patch.object(
            bot, "eleitor_tse_autenticado", side_effect=[True, False]
        ):
            self.assertTrue(bot.desautenticar_eleitor_tse(page))
        link.scroll_into_view_if_needed.assert_called_once_with(timeout=5000)
        link.click.assert_called_once_with(timeout=8000)

    def test_consulta_nao_abre_onde_votar_com_eleitor_anterior_ativo(self):
        page = MagicMock()
        with patch.object(bot, "ir_para", return_value=True), patch.object(
            bot, "eleitor_tse_autenticado", return_value=True
        ), patch.object(bot, "desautenticar_eleitor_tse", return_value=False), patch.object(
            bot, "abrir_onde_votar"
        ) as abrir, patch.object(bot, "aguardar_pausa", return_value=True):
            resultado = bot.consultar_tse_playwright_page(page, PESSOA)
        self.assertFalse(resultado.resposta_do_tse)
        abrir.assert_not_called()

    def test_erro_na_consulta_tambem_volta_tse_para_home(self):
        crm = MagicMock()
        tse_context = MagicMock()
        with patch.object(bot, "abrir_pessoa_por_cpf", return_value=PESSOA), patch.object(
            bot, "consultar_tse", side_effect=RuntimeError("falha de teste")
        ), patch.object(bot, "voltar_tse_para_home") as voltar_home, patch.object(
            bot, "voltar_para_pendentes"
        ):
            with self.assertRaisesRegex(RuntimeError, "falha de teste"):
                bot.tratar_pessoa(MagicMock(), MagicMock(), crm, tse_context, PESSOA)
        voltar_home.assert_called_once_with(tse_context)

    def test_main_abre_tse_uma_vez_para_a_fila_e_fecha_no_final(self):
        pw = MagicMock()
        gerenciador = MagicMock()
        gerenciador.__enter__.return_value = pw
        crm_context = MagicMock()
        tse_context = MagicMock()
        crm_page = MagicMock()
        tse_page = MagicMock()
        tse_page.is_closed.return_value = False
        crm_context.pages = [crm_page]
        tse_context.pages = [tse_page]
        with patch.object(bot, "perguntar_operador", return_value=(0, 1, 1)), patch.object(
            bot, "sync_playwright", return_value=gerenciador
        ), patch.object(
            bot, "abrir_contexto_navegador", side_effect=[crm_context, tse_context]
        ) as abrir, patch.object(bot, "ir_para", return_value=True), patch.object(
            bot, "ensure_crm_ready"
        ), patch.object(
            bot, "inventariar_com_retentativa", return_value=[PESSOA]
        ), patch.object(bot, "fatia_do_cpf", return_value=0), patch.object(
            bot, "rodar_fila", return_value=[]
        ) as rodar:
            bot.main()
        self.assertEqual(
            abrir.call_args_list,
            [call(pw, tse=False), call(pw, tse=True)],
        )
        rodar.assert_called_once_with(pw, crm_context, crm_page, tse_context, [PESSOA], "")
        tse_context.close.assert_called_once()
        crm_context.close.assert_called_once()

    def test_main_porta_tse_ocupada_encerra_sem_processar_nem_salvar(self):
        pw = MagicMock()
        gerenciador = MagicMock()
        gerenciador.__enter__.return_value = pw
        crm_context = MagicMock()
        crm_page = MagicMock()
        crm_context.pages = [crm_page]
        with patch.object(bot, "perguntar_operador", return_value=(0, 1, 1)), patch.object(
            bot, "sync_playwright", return_value=gerenciador
        ), patch.object(
            bot, "abrir_contexto_navegador",
            side_effect=[crm_context, RuntimeError("Porta local 9223 ocupada")],
        ), patch.object(bot, "ir_para", return_value=True), patch.object(
            bot, "ensure_crm_ready"
        ), patch.object(
            bot, "inventariar_com_retentativa", return_value=[PESSOA]
        ), patch.object(bot, "fatia_do_cpf", return_value=0), patch.object(
            bot, "rodar_fila"
        ) as rodar:
            bot.main()
        rodar.assert_not_called()
        crm_context.close.assert_called_once()

    def test_contexto_tse_fecha_em_erro_e_interrupcao(self):
        for erro in (RuntimeError("falha"), KeyboardInterrupt()):
            with self.subTest(erro=type(erro).__name__):
                context = MagicMock()
                with patch.object(bot, "abrir_contexto_navegador", return_value=context), patch.object(
                    bot, "consultar_tse_playwright_page", side_effect=erro
                ):
                    with self.assertRaises(type(erro)):
                        bot.consultar_tse_chrome_normal(MagicMock(), PESSOA)
                    context.close.assert_called_once()

    def test_intervalo_tambem_apos_excecao_da_consulta(self):
        relogio = Relogio()
        crm = MagicMock()
        crm.wait_for_timeout.side_effect = relogio.esperar
        context = MagicMock(pages=[crm])
        with patch.object(bot, "_PROXIMA_CONSULTA_TSE", 0), patch.object(
            bot.time, "monotonic", side_effect=lambda: relogio.agora
        ), patch.object(bot, "consultar_tse_chrome_normal", side_effect=[RuntimeError("falha"), MagicMock()]):
            with self.assertRaises(RuntimeError):
                bot.consultar_tse(None, context, PESSOA)
            bot.consultar_tse(None, context, PESSOA)
            self.assertEqual(relogio.agora, 30)

    def test_falhas_de_navegacao_aplicam_backoff_e_nao_viram_nao_achei(self):
        page = MagicMock()
        with patch.object(bot, "ir_para", return_value=False) as navegar, patch.object(
            bot, "aguardar_pausa", return_value=True
        ) as pausa:
            resultado = bot.consultar_tse_playwright_page(page, PESSOA)
            self.assertFalse(resultado.resposta_do_tse)
            self.assertFalse(resultado.encontrado)
            self.assertEqual([c.args[1] for c in pausa.call_args_list], [60000, 120000])
            self.assertEqual(navegar.call_count, 3)
            self.assertTrue(all(c.kwargs["tentativas"] == 1 for c in navegar.call_args_list))
            page.locator.assert_not_called()

    def test_erro_captcha_nao_clica_reenvio(self):
        page = MagicMock()
        bot.limpar_erro_captcha(page)
        self.assertEqual(page.mock_calls, [])

    def test_digitacao_incompleta_nao_envia_formulario(self):
        page = MagicMock()
        page.locator.return_value.first.input_value.return_value = "000"
        with patch.object(bot, "aguardar_pausa"), patch.object(bot, "wait_tse_loading"):
            self.assertFalse(bot.preencher_autenticacao(page, PESSOA))
            page.get_by_role.assert_not_called()

    def test_clica_e_limpa_com_teclado_antes_de_digitar(self):
        page = MagicMock()
        campo = page.locator.return_value.first
        campo.input_value.return_value = PESSOA.mae
        with patch.object(bot, "aguardar_pausa") as pausa:
            bot.fill_first_locator(page, ("#nomeMae",), PESSOA.mae)
        campo.assert_has_calls([
            call.scroll_into_view_if_needed(timeout=8000),
            call.click(timeout=8000),
            call.press("ControlOrMeta+A", timeout=8000),
            call.press("Backspace", timeout=8000),
            call.press_sequentially(PESSOA.mae, delay=bot.TSE_DIGITACAO_MS, timeout=15000),
            call.press("Tab", timeout=8000),
        ])
        pausa.assert_has_calls([
            call(page, bot.TSE_PAUSA_APOS_CLIQUE_MS),
            call(page, bot.TSE_PAUSA_ENTRE_CAMPOS_MS),
        ])

    def test_crm_fecha_se_inventario_falhar(self):
        pw = MagicMock()
        context = MagicMock()
        with patch.object(bot, "perguntar_operador", return_value=(0, 1, 1)), patch.object(
            bot, "sync_playwright", return_value=pw
        ), patch.object(bot, "abrir_contexto_navegador", return_value=context), patch.object(
            bot, "ir_para", return_value=True
        ), patch.object(bot, "ensure_crm_ready"), patch.object(
            bot, "inventariar_com_retentativa", side_effect=RuntimeError("falha")
        ):
            with self.assertRaises(RuntimeError):
                bot.main()
            context.close.assert_called_once()


class ChromeExternoTests(unittest.TestCase):
    def test_aguarda_liberacao_da_porta_apos_encerrar(self):
        with patch.object(chrome_normal, "porta_livre", side_effect=[False, False, True]), patch.object(
            chrome_normal.time, "sleep"
        ) as pausa:
            chrome_normal.esperar_porta_livre(9224)
            self.assertEqual(pausa.call_count, 2)

    def test_limpeza_temporaria_repete_apos_arquivo_ocupado(self):
        temporario = MagicMock()
        temporario.cleanup.side_effect = [PermissionError("ocupado"), None]
        with patch.object(chrome_normal.time, "sleep"):
            chrome_normal.limpar_temporario(temporario)
        self.assertEqual(temporario.cleanup.call_count, 2)

    def test_porta_ocupada_nao_abre_nem_fecha_outro_navegador(self):
        with patch.object(chrome_normal, "porta_livre", return_value=False), patch.object(
            chrome_normal.subprocess, "Popen"
        ) as abrir:
            with self.assertRaisesRegex(RuntimeError, "ocupada"):
                chrome_normal.abrir_chrome_normal(
                    MagicMock(), executable_path="chrome", profile_dir=Path("nao-usar"),
                    clean=False, slow_mo=500,
                )
            abrir.assert_not_called()

    def test_abre_processo_conecta_cdp_e_fecha_graciosamente(self):
        pw = MagicMock()
        processo = MagicMock()
        browser = pw.chromium.connect_over_cdp.return_value
        contexto = MagicMock()
        browser.contexts = [contexto]
        with TemporaryDirectory(prefix="crm-tse-unit-") as pasta, patch.object(
            chrome_normal, "porta_livre", return_value=True
        ), patch.object(chrome_normal, "esperar_cdp"), patch.object(
            chrome_normal.subprocess, "Popen", return_value=processo
        ) as abrir:
            sessao = chrome_normal.abrir_chrome_normal(
                pw, executable_path="chrome.exe", profile_dir=Path(pasta), clean=False, slow_mo=500,
            )
            comando = abrir.call_args.args[0]
            self.assertEqual(comando[0], "chrome.exe")
            self.assertIn("--remote-debugging-port=9223", comando)
            self.assertEqual(comando[-1], "about:blank")
            pw.chromium.connect_over_cdp.assert_called_once_with(
                "http://127.0.0.1:9223", slow_mo=500, timeout=15000
            )
            pw.chromium.launch_persistent_context.assert_not_called()
            self.assertIs(sessao.pages, contexto.pages)
            sessao.close()
            sessao.close()
            browser.new_browser_cdp_session.return_value.send.assert_called_once_with("Browser.close")
            processo.wait.assert_called_once_with(timeout=5)
            processo.terminate.assert_not_called()

    def test_falha_ao_conectar_encerra_apenas_processo_criado(self):
        pw = MagicMock()
        pw.chromium.connect_over_cdp.side_effect = RuntimeError("conexao falhou")
        processo = MagicMock()
        with TemporaryDirectory(prefix="crm-tse-unit-") as pasta, patch.object(
            chrome_normal, "porta_livre", return_value=True
        ), patch.object(chrome_normal, "esperar_cdp"), patch.object(
            chrome_normal.subprocess, "Popen", return_value=processo
        ), patch.object(chrome_normal, "encerrar_chrome") as encerrar:
            with self.assertRaisesRegex(RuntimeError, "conexao falhou"):
                chrome_normal.abrir_chrome_normal(
                    pw, executable_path="chrome.exe", profile_dir=Path(pasta), clean=False, slow_mo=500,
                )
            encerrar.assert_called_once_with(None, processo)

    def test_chrome_que_ja_encerrou_nao_usa_endpoint_antigo(self):
        processo = MagicMock()
        processo.poll.return_value = 0
        with patch.object(chrome_normal.urllib.request, "build_opener") as opener:
            with self.assertRaisesRegex(RuntimeError, "perfil pode estar em uso"):
                chrome_normal.esperar_cdp(processo, "http://127.0.0.1:9223")
            opener.return_value.open.assert_not_called()


@unittest.skipUnless(os.environ.get("TESTAR_CHROME") == "1", "Teste de Chrome opcional")
class ChromeLocalTests(unittest.TestCase):
    def test_formulario_cliques_e_cookies_entre_aberturas(self):
        with socket.socket() as reserva:
            reserva.bind(("127.0.0.1", 0))
            porta_teste = reserva.getsockname()[1]
        html = '''<!doctype html><html><body>
        <input id="titulo-cpf-nome" value="valor antigo" onmousedown="this.dataset.clicado='sim'">
        <input aria-label="Data de nascimento" onmousedown="this.dataset.clicado='sim'">
        <input id="nomeMae" onmousedown="this.dataset.clicado='sim'">
        <button onclick="document.getElementById('estado').textContent='ENVIADO'">Entrar</button>
        <p id="estado">AGUARDANDO</p></body></html>'''
        # Perfis exclusivos do teste. Nunca abre nem limpa os perfis de uso real.
        with patch.object(bot, "TSE_REMOTE_DEBUGGING_PORT", porta_teste), TemporaryDirectory(prefix="crm-tse-teste-") as pasta, patch.object(
            bot, "TSE_PROFILE_DIR", Path(pasta) / "perfil"
        ), patch.object(bot, "HEADLESS", True), patch.object(bot, "TSE_SLOW_MO_MS", 0), patch.object(
            bot, "TSE_PAUSA_ENTRE_CAMPOS_MS", 0
        ), patch.object(bot, "TSE_PAUSA_APOS_CLIQUE_MS", 0), patch.object(
            bot, "TSE_PAUSA_ANTES_ENVIAR_MS", 0
        ), bot.sync_playwright() as pw:
            for limpo in (False, True):
                with self.subTest(perfil_limpo=limpo), patch.object(bot, "TSE_PERFIL_LIMPO", limpo):
                    context = bot.abrir_contexto_navegador(pw, tse=True)
                    try:
                        context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
                        page = context.pages[0]
                        page.goto("https://formulario-teste.invalid/")
                        agente = page.evaluate("navigator.userAgent")
                        self.assertIn("Edg/" if bot.TSE_NAVEGADOR == "edge" else "Chrome/", agente)
                        self.assertTrue(bot.preencher_autenticacao(page, PESSOA))
                        self.assertEqual(page.locator("#titulo-cpf-nome").input_value(), PESSOA.cpf)
                        self.assertEqual(page.get_by_label("Data de nascimento").input_value(), PESSOA.nascimento)
                        self.assertEqual(page.locator("#nomeMae").input_value(), PESSOA.mae)
                        for campo in page.locator("input").all():
                            self.assertEqual(campo.get_attribute("data-clicado"), "sim")
                        self.assertEqual(page.locator("#estado").inner_text(), "ENVIADO")
                        # Apenas cookie ficticio criado neste teste; nenhum perfil pessoal.
                        page.evaluate("document.cookie = 'teste=1; max-age=3600; path=/'")
                        self.assertEqual(page.evaluate("document.cookie"), "teste=1")
                    finally:
                        context.close()
                    novo = bot.abrir_contexto_navegador(pw, tse=True)
                    try:
                        novo.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
                        page = novo.pages[0]
                        page.goto("https://formulario-teste.invalid/")
                        self.assertEqual(page.evaluate("document.cookie"), "" if limpo else "teste=1")
                    finally:
                        novo.close()

    def test_mesma_janela_atende_duas_pessoas_e_mantem_cookie(self):
        with socket.socket() as reserva:
            reserva.bind(("127.0.0.1", 0))
            porta_teste = reserva.getsockname()[1]
        html = '''<!doctype html><html><body>
        <p id="perfil">Eleitor / Eleitora com CPF nº 000.000.000-00</p>
        <a href="#" onclick="document.getElementById('perfil').remove(); this.remove()">(Não sou este eleitor)</a>
        <input id="titulo-cpf-nome"><input aria-label="Data de nascimento">
        <input id="nomeMae"><button onclick="document.getElementById('estado').textContent='ENVIADO'">Entrar</button>
        <div role="dialog"><button onclick="document.getElementById('estado').textContent='SAIU'">Sair</button></div>
        <p id="estado">AGUARDANDO</p></body></html>'''
        segunda = bot.Pessoa(1, "OUTRA PESSOA", "11111111111", "OUTRA MAE", "02/02/2001")
        with patch.object(bot, "TSE_REMOTE_DEBUGGING_PORT", porta_teste), TemporaryDirectory(prefix="crm-tse-sessao-") as pasta, patch.object(
            bot, "TSE_PROFILE_DIR", Path(pasta) / "perfil"
        ), patch.object(bot, "HEADLESS", True), patch.object(bot, "TSE_SLOW_MO_MS", 0), patch.object(
            bot, "TSE_PAUSA_ENTRE_CAMPOS_MS", 0
        ), patch.object(bot, "TSE_PAUSA_APOS_CLIQUE_MS", 0), patch.object(
            bot, "TSE_PAUSA_ANTES_ENVIAR_MS", 0
        ), bot.sync_playwright() as pw:
            context = bot.abrir_contexto_navegador(pw, tse=True)
            try:
                context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
                page = context.pages[0]
                page.goto("https://sessao-teste.invalid/")
                self.assertTrue(bot.preencher_autenticacao(page, PESSOA))
                page.evaluate("document.cookie = 'sessao=mantida; max-age=3600; path=/'")

                # Simula a volta para a home e a proxima consulta sem fechar o contexto.
                page.goto("https://sessao-teste.invalid/")
                self.assertEqual(page.evaluate("document.cookie"), "sessao=mantida")
                self.assertTrue(bot.preencher_autenticacao(page, segunda))
                self.assertEqual(page.locator("#titulo-cpf-nome").input_value(), segunda.cpf)
                self.assertEqual(page.locator("#nomeMae").input_value(), segunda.mae)
                self.assertTrue(bot.sair_do_perfil_tse(page))
                self.assertEqual(page.locator("#estado").inner_text(), "SAIU")
                self.assertTrue(bot.desautenticar_eleitor_tse(page))
                self.assertFalse(bot.eleitor_tse_autenticado(page))
                self.assertIs(bot.pagina_tse(context), page)
            finally:
                context.close()


if __name__ == "__main__":
    unittest.main()
