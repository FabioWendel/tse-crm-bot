# CRM + TSE — versão lenta com perfil persistente

Cópia independente do código original. Não foram copiados dados de eleitores,
histórico, cookies, login ou arquivos de consultas anteriores.
O padrão agora é manter os perfis desta versão: não limpar cookies nem histórico
ao trocar de pessoa ou reiniciar o programa. Uma única janela do TSE permanece
aberta durante toda a rodada. Depois de cada pessoa, ela volta à página inicial
do autoatendimento: primeiro aciona **Sair** no modal/perfil do eleitor e depois
aciona **Não sou este eleitor** na página principal. O próximo fluxo de
**Onde votar** só começa depois de confirmar que o eleitor anterior desapareceu.
Usa Brave, Chrome ou Edge instalado, em janela visível — sem reutilizar o perfil
pessoal desses navegadores.

**Abertura do TSE corrigida:** o programa inicia `brave.exe`, `chrome.exe` ou `msedge.exe` como um processo
separado e só depois conecta o Playwright por CDP, como fazia a versão original.
Não usa `launch_persistent_context` para abrir o TSE. O CRM continua sendo aberto
pelo Playwright e pode mostrar a faixa de controle automatizado. A conexão CDP
continua sendo automação; não é uma promessa de invisibilidade ao site.

## Executar no Windows

Para uma comparação totalmente manual no seu Chrome pessoal, use
`ABRIR-TSE-NO-CHROME-PESSOAL.cmd`. Esse atalho apenas abre a página do TSE no
Chrome instalado, sem Playwright, CDP ou acesso programático a cookies. Ele não
participa da fila do CRM e o resultado precisa ser tratado manualmente.

O padrão agora é **Brave no TSE**: use **INICIAR.cmd** ou **INICIAR-BRAVE.cmd**.
Para escolher outro navegador, use **INICIAR-CHROME.cmd** ou **INICIAR-EDGE.cmd**.

No macOS, usando o ambiente virtual criado na raiz do projeto:

```bash
cd versao-lenta
TSE_NAVEGADOR=brave ../.venv/bin/python crm_tse_bot.py
```
O CRM permanece no Chrome em todos os casos. Não rode mais de um atalho ao
mesmo tempo na mesma fila.

O Brave foi localizado nesta máquina em
`C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe`. Ele usa
perfil persistente exclusivo `.tse-brave-profile/` e porta local 9225. Isso não
ativa stealth e não garante menos CAPTCHA.

O Edge já está instalado nesta máquina. Ele usa um perfil exclusivo persistente
em `.tse-edge-profile/` e a porta local 9224; não copia cookies nem histórico do
Chrome. As pausas e o preenchimento são os mesmos. Comece com limite de 1 pessoa.
Essa alternativa permite comparar funcionamento, mas **não foi comprovado que
o Edge peça menos CAPTCHA**. Nenhuma configuração de segurança foi desativada.

Nesta máquina, o atalho usa o Python já
instalado na pasta `.venv312` do projeto original. O código executado é o desta
nova pasta, e os relatórios também ficam aqui.

Para diagnosticar o ambiente, sem acessar CRM ou TSE, execute no PowerShell:

```powershell
..\.venv312\Scripts\python.exe crm_tse_bot.py --teste
```

Para diagnosticar o Edge sem acessar CRM/TSE: `INICIAR-EDGE.cmd --teste`.
O diagnóstico abre páginas vazias e verifica a conexão com o navegador externo
usando um perfil temporário exclusivo do teste, sem tocar no perfil de uso real.
Se o Edge não existir em outra máquina, o bot informa isso; não troca de navegador
silenciosamente nem instala programas.

Se copiar a pasta para outra máquina, instale Python 3.12 e Google Chrome, depois:

```powershell
py -3.12 -m venv .venv312
.\.venv312\Scripts\python.exe -m pip install -r requirements.txt
```

O programa mantém o fluxo do original: inventaria os pendentes, preenche o TSE
com CPF, nascimento e nome da mãe, lê o resultado e grava no CRM. As regras de
classificação e inativação foram mantidas. **Não é apenas um demonstrador de
preenchimento: ao rodar normalmente, ele faz alterações reais no CRM.**

Se nome da mãe ou nascimento estiver ausente, o programa não abre o TSE e marca
"Não achei" diretamente no CRM. Uma linha sem CPF fica intocada porque não pode
ser reencontrada com segurança.

Para dividir **Pendentes** entre quatro máquinas, escolha a opção `2` na primeira
pergunta. Essa opção mantém a aba Pendentes e divide os CPFs em quatro blocos
estáveis (`0` a `3`). Rode todos os blocos para cobrir a lista inteira; no teto,
`Enter` ou `0` não limita o bloco. Na mesma máquina, rode um bloco por vez; o
paralelismo deve usar máquinas diferentes porque os processos compartilham
perfil e porta do navegador.

No Windows, abra `INICIAR-BRAVE.cmd`, escolha a divisão `2`, informe um bloco de
`0` a `3` e pressione `Enter` no teto para percorrer o bloco inteiro. Repita o
procedimento com os quatro números para cobrir todos os cadastros.

Faça primeiro uma rodada com limite de **1 pessoa**, usando dados cuja consulta
você está autorizado a realizar. Se surgir CAPTCHA, resolva manualmente na janela.
Não execute as duas versões sobre a mesma fatia de pessoas ao mesmo tempo.

## O que mudou

| Ajuste | Padrão |
|---|---|
| Intervalo entre consultas | Pelo menos 30 s após terminar a consulta anterior; o tempo de gravação no CRM conta nesse intervalo |
| Foco dos campos | Rola até o campo, clica com o mouse e espera 0,5 s; seleciona e apaga o texto pelo teclado |
| Digitação no TSE | 220 ms por caractere |
| Pausa entre campos | 2,5 s |
| Pausa antes de enviar | 5 s |
| Operações do navegador do TSE | 500 ms adicionais por operação |
| Repetição após falha | 60 s antes da segunda tentativa; 120 s antes da terceira, respeitando o teto de tempo |
| Repasse automático ao final | Desativado; falhas ficam para outra rodada |
| Perfil do CRM | Persistente em `.browser-profile/` desta versão |
| Perfil do TSE | Persistente em `.tse-brave-profile/`, `.tse-chrome-profile/` ou `.tse-edge-profile/`; uma janela por rodada, fechada somente ao terminar ou interromper a execução |

As configurações ficam no começo de `crm_tse_bot.py`.
`CRM_PERFIL_LIMPO = False` e `TSE_PERFIL_LIMPO = False` são os padrões e preservam
sessão **somente nos perfis desta nova pasta**, sem acessar os perfis originais.
No primeiro uso do perfil persistente será necessário fazer login no CRM. Nas
aberturas seguintes o site poderá reaproveitar a sessão, até expirar ou ser revogada.
Se alguma página pedir autenticação adicional ou CAPTCHA, faça essa etapa na
própria janela; o programa não envia credenciais nem resolve o desafio sozinho.
Não abra duas instâncias desta versão ao mesmo tempo usando os mesmos perfis.

Não é necessário entrar em uma conta Google para consultar o TSE. O programa
não automatiza login Google nem usa uma conta pessoal para tentar alterar a
detecção do site. Se você decidir entrar manualmente nesse perfil por outro
motivo, lembre que os dados dessa conta ficarão armazenados no perfil dedicado
da automação até você removê-los.

Somente se você mudar alguma opção `PERFIL_LIMPO` para `True`, o Playwright usará
um perfil temporário para aquele navegador, removido no encerramento normal.
Em encerramento forçado podem sobrar arquivos temporários. Os CSVs e logs são
mantidos independentemente dessa opção. O programa não apaga o histórico do
Chrome pessoal e não torna a conexão anônima.

No fluxo normal, cada janela do TSE abre diretamente no Autoatendimento. O
`about:blank` permanece apenas como padrão interno para chamadas de diagnóstico.
A conexão usa
a porta local **9225** no Brave, **9223** no Chrome ou **9224** no Edge, configurável em `TSE_REMOTE_DEBUGGING_PORT`, diferente da
9222 usada pelo original. Se estiver ocupada, o bot informa o conflito sem
conectar em outra janela nem fechá-la. Antes de reiniciar esta versão, feche a
execução anterior e a janela do TSE que ela abriu.

Ao finalizar **a rodada**, o programa pede o encerramento normal ao Chrome para
preservar o perfil. Entre pessoas, ele mantém o mesmo processo, contexto, página,
cookies e sessão, navegando de volta à página inicial do TSE. Se o processo
travar, o encerramento forçado é limitado ao
processo que esta execução criou, sem procurar ou matar outros Chromes por nome.
Esse cuidado também é aplicado em falhas da consulta.
No Edge, aguarda também a liberação da porta de controle antes de reabrir o
perfil; os processos auxiliares podem demorar um pouco mais a encerrar.

## Sobre CAPTCHA

Pausas, cliques e perfil persistente **não garantem menos CAPTCHA**. Não foi
confirmado que a limpeza de cookies tenha causado os desafios anteriores.
Um perfil novo perde a sessão anterior. Os intervalos são valores iniciais
de teste, não limites oficiais publicados pelo TSE. Não há disfarce de navegador,
troca de IP, resolução automática de CAPTCHA ou simulação de identidade humana.

A versão aguarda resolução manual quando o CAPTCHA é detectado, dentro dos
limites de espera do original. Se a consulta não completar, não marca “Não achei”
por esse motivo (`MARCAR_NAO_ACHEI_EM_ERRO_TECNICO = False`).

APIs usadas: [perfis do navegador no Playwright](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)
e [digitação sequencial](https://playwright.dev/python/docs/api/class-locator#locator-press-sequentially).
O Playwright também suporta os binários instalados de [Chrome e Microsoft Edge](https://playwright.dev/python/docs/browsers#google-chrome--microsoft-edge).

## Verificação local

```powershell
..\.venv312\Scripts\python.exe -m unittest discover -s tests -v
```

Para incluir o teste de navegador com formulário fictício servido localmente
pelo próprio teste (sem consultar TSE nem acessar CRM):

```powershell
$env:TESTAR_CHROME = '1'
..\.venv312\Scripts\python.exe -m unittest discover -s tests -v
Remove-Item Env:TESTAR_CHROME
```

O terminal mostra um resumo de cada resultado; o conteúdo completo também fica
em `execucao_detalhada.txt` para evitar excesso de texto na tela.

O CRM usa até 50 linhas por página e é renovado preventivamente a cada 50
pessoas, somente entre consultas. Se a tela de Pendentes não voltar, o restante
da fila é preservado.

`consultas.csv`, `execucao_detalhada.txt`, planilhas e logs podem conter dados pessoais. Não versione nem
compartilhe esses arquivos sem autorização. A pasta nova começa sem esses dados.
