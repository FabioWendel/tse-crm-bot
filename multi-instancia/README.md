# Quatro terminais no mesmo computador

Esta versao usa o mesmo fluxo da versao normal, mas separa os recursos que
causariam conflito entre processos:

- perfil do Chrome usado no CRM;
- perfil do Brave usado no TSE;
- porta local de controle do Brave;
- `consultas.csv`, `bot_error.log` e `execucao_detalhada.txt`.

## Como executar

Na primeira utilizacao, feche as versoes normal e lenta. Depois abra:

```text
INICIAR-4-TERMINAIS.cmd
```

Serão abertas quatro janelas, já fixadas como operadores `0`, `1`, `2` e `3`.
Em cada uma, informe apenas o teto e a pausa. Use `0` no teto para percorrer a
fatia inteira.

Cada janela tem um perfil novo do CRM. No primeiro uso, faça o login nas quatro
janelas. Não feche uma janela de navegador que pertença a um operador ainda em
execução.

Também é possível iniciar apenas uma janela pelos arquivos
`INICIAR-OPERADOR-0.cmd` até `INICIAR-OPERADOR-3.cmd`.

## Capacidade recomendada

Quatro operadores abrem vários processos do Chrome e do Brave. Recomenda-se no
mínimo 16 GB de RAM; 24 ou 32 GB oferecem mais folga. Comece com dois operadores
e observe o Gerenciador de Tarefas antes de abrir os outros dois.

Todos os dados ficam em `multi-instancia/dados/operador-N/`. Essa pasta contém
sessões autenticadas e dados pessoais e está excluída do Git.

O CAPTCHA continua manual. Esta versão não adiciona stealth, proxy ou solução
automática de CAPTCHA.
