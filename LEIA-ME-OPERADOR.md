# CRM x TSE — instruções para o operador

## Antes de começar

Você precisa de:

- **Google Chrome instalado** (o programa usa o Chrome da sua máquina).
- **Internet.**
- **Seu login do CRM.**

Não precisa instalar Python nem nada além disso. É um arquivo só.

## Instalação — Windows

1. Crie uma pasta, por exemplo `C:\CRM-TSE`.
2. Coloque o `CRM-TSE-Bot.exe` dentro dela.
3. Pronto.

## Instalação — Mac

1. Abra o `CRM-TSE-Bot.dmg`.
2. Arraste a pasta inteira para algum lugar seu (Documentos, por exemplo).
3. **No primeiro uso:** clique com o botão direito no `CRM-TSE-Bot.command`
   e escolha **Abrir**. O Mac vai avisar que o programa não tem assinatura —
   confirme. Isso só é necessário uma vez; depois basta dar dois cliques.

Nos dois casos vale a mesma regra abaixo.

> Deixe o `.exe` numa pasta sua, **não** na Área de Trabalho nem em Downloads.
> Ele cria arquivos ao lado de si mesmo (o registro das consultas e o login
> salvo do navegador).

## Primeiro uso — teste rápido

Antes de rodar valendo, confira se está tudo certo. Abra o Prompt de Comando
na pasta e rode:

```bash
CRM-TSE-Bot.exe --teste
```

Deve terminar com "Tudo certo". Se falhar no passo 1, o Chrome não está
instalado (ou está num lugar fora do padrão).

## Uso normal

Dê **dois cliques** no `CRM-TSE-Bot.exe` (no Mac, no `CRM-TSE-Bot.command`).
Ele vai perguntar:

```
Quantos operadores vao rodar agora (1 = so voce)?
Voce e o operador numero (0 a 9)?
```

**Combine esses dois números com a equipe antes de começar.** Todo mundo
informa o **mesmo total**, e cada um informa **um número diferente**. É isso
que faz cada pessoa pegar uma parte diferente da fila, sem repetir trabalho.

Exemplo com 4 pessoas: todos respondem `4` na primeira pergunta; a primeira
pessoa responde `0`, a segunda `1`, a terceira `2`, a quarta `3`.

> **Importante:** só as fatias que forem rodadas são processadas. Se vocês
> combinarem 10 mas só 3 rodarem, cerca de 70% da fila fica sem tratamento.
> Informe o número de pessoas que **realmente** vão rodar agora.

Se você for rodar sozinho, responda `1` — ele processa a fila inteira.

## O que acontece depois

1. Abre o Chrome no CRM. **Faça login se ele pedir.**
2. Ele conta quantas pessoas estão pendentes e separa a sua parte.
3. Para cada pessoa: abre o TSE, preenche os dados e **espera você resolver o
   CAPTCHA**. Resolva e a consulta segue sozinha.
4. Grava o local de votação no CRM, ou marca "Não achei" quando o TSE não
   localiza a pessoa.

Pode interromper quando quiser: feche a janela ou aperte `Ctrl+C`. O que já
foi gravado continua salvo.

## Arquivos que aparecem na pasta

| Arquivo | O que é |
|---|---|
| `consultas.csv` | registro de tudo que foi consultado |
| `bot_error.log` | detalhe do último erro, se acontecer |
| `.browser-profile/` | seu login do CRM salvo, para não logar toda vez |
| `.tse-chrome-profile/` | sessão do TSE |

⚠️ **O `consultas.csv` tem CPF, nome da mãe e endereço de eleitores.** Não
mande esse arquivo por WhatsApp, e-mail ou grupo. Se precisar entregá-lo,
combine antes com o responsável.

## Quando der problema

**"Não abre nada" / a janela fecha sozinha** — rode `CRM-TSE-Bot.exe --teste`
pelo Prompt de Comando e mande um print do resultado.

**O antivírus ou o Windows bloqueou** — é comum com programa novo sem
assinatura digital. Em "Mais informações" → "Executar assim mesmo". Se o
antivírus apagar o arquivo, peça para o responsável liberar.

**Parou no meio** — é só abrir de novo. Quem já foi tratado sai da fila
automaticamente, não repete.

**Deu erro na tela** — mande o `bot_error.log` para o responsável.
