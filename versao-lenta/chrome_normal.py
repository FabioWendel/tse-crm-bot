"""Abre Chrome ou Edge como processo externo e depois conecta Playwright por CDP."""

import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import Error


def porta_livre(porta: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", porta))
            return True
        except OSError:
            return False


def esperar_cdp(processo, endereco: str, timeout: float = 15) -> None:
    # A conexao e exclusivamente local; nao depende do proxy configurado no SO.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if processo.poll() is not None:
            raise RuntimeError(
                "O navegador do TSE encerrou antes da conexao. Feche a execucao anterior "
                "desta versao e tente novamente; o perfil pode estar em uso."
            )
        try:
            with opener.open(endereco + "/json/version", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("O navegador abriu, mas a conexao de controle local nao respondeu.")


def esperar_porta_livre(porta: int, timeout: float = 10) -> None:
    # No Edge o processo inicial pode sair antes dos processos do navegador.
    # Aguarda a liberacao real da porta antes de reutilizar o mesmo perfil.
    limite = time.monotonic() + timeout
    while not porta_livre(porta):
        if time.monotonic() >= limite:
            raise RuntimeError(
                "O navegador ainda esta encerrando. Feche a janela do TSE desta "
                "execucao antes de tentar novamente; o perfil foi preservado."
            )
        time.sleep(0.2)


def limpar_temporario(temporario) -> None:
    if temporario is None:
        return
    # O Windows pode demorar a liberar arquivos de cache apos fechar a janela.
    for tentativa in range(6):
        try:
            temporario.cleanup()
            return
        except OSError:
            if tentativa == 5:
                print("Aviso: o perfil temporario ainda esta ocupado; nao sera reutilizado.")
                return
            time.sleep(0.5)


def encerrar_chrome(browser, processo) -> None:
    if browser is not None:
        try:
            # Browser.close() do Playwright via CDP so desconecta. O comando
            # abaixo pede ao Chrome que encerre normalmente e grave o perfil.
            browser.new_browser_cdp_session().send("Browser.close")
        except Error:
            pass
        finally:
            try:
                browser.close()
            except Error:
                pass
    if processo is not None:
        try:
            processo.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Ultimo recurso: somente o processo criado por esta chamada.
            # Nunca faz taskkill/pkill por nome ou busca por perfil de terceiros.
            processo.terminate()
            try:
                processo.wait(timeout=5)
            except subprocess.TimeoutExpired:
                processo.kill()
                processo.wait(timeout=5)


class ContextoChromeNormal:
    """Encaminha a API do contexto e fecha tambem o Chrome externo que abriu."""

    def __init__(self, context, browser, processo, temporario, porta):
        self._context = context
        self._browser = browser
        self._processo = processo
        self._temporario = temporario
        self._porta = porta
        self._fechado = False

    def __getattr__(self, name):
        return getattr(self._context, name)

    def close(self):
        if self._fechado:
            return
        encerrar_chrome(self._browser, self._processo)
        esperar_porta_livre(self._porta)
        self._fechado = True
        limpar_temporario(self._temporario)


def abrir_chrome_normal(
    playwright, *, executable_path: str, profile_dir: Path, clean: bool,
    slow_mo: int, headless: bool = False, port: int = 9223,
) -> ContextoChromeNormal:
    if not porta_livre(port):
        raise RuntimeError(
            f"Porta local {port} ocupada. Feche a execucao anterior da versao lenta "
            "ou configure outra TSE_REMOTE_DEBUGGING_PORT. Nenhum navegador foi fechado."
        )
    temporario = TemporaryDirectory(prefix="crm-tse-chrome-") if clean else None
    processo = None
    browser = None
    try:
        perfil = Path(temporario.name) if temporario is not None else profile_dir.resolve()
        perfil.mkdir(parents=True, exist_ok=True)
        comando = [
            executable_path,
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={perfil}",
            "--no-first-run",
            "--new-window",
        ]
        if headless:
            comando.append("--headless=new")  # somente diagnosticos/testes
        # O laco da consulta faz a unica navegacao ao TSE quando estiver pronto.
        comando.append("about:blank")
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform.startswith("win") else {"start_new_session": True}
        processo = subprocess.Popen(
            comando, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs
        )
        endereco = f"http://127.0.0.1:{port}"
        esperar_cdp(processo, endereco)
        browser = playwright.chromium.connect_over_cdp(endereco, slow_mo=slow_mo, timeout=15000)
        if not browser.contexts:
            raise RuntimeError("O navegador nao disponibilizou o contexto esperado.")
        return ContextoChromeNormal(browser.contexts[0], browser, processo, temporario, port)
    except BaseException:
        encerrar_chrome(browser, processo)
        if processo is not None:
            esperar_porta_livre(port)
        limpar_temporario(temporario)
        raise
