# LMF Control

Aplicação local para gestão operacional da LMF Informática, com FastAPI, banco persistente JSON e interface em português. O banco é criado automaticamente em `%LOCALAPPDATA%\LMF-Control\data\lmf_control.json` e permanece entre reinicializações, fora dos arquivos do projeto. O arquivo JSON é a única fonte de dados persistente; o adaptador relacional roda apenas em memória para manter as operações transacionais existentes.

A tela inicial usa cartões e listas com indicadores calculados dos registros locais. Clientes continuam armazenados e vinculados às ordens e vendas, mas a aba dedicada foi removida da navegação. O seletor no topo alterna entre os temas claro e escuro e memoriza a preferência neste navegador. A busca global abre com `Ctrl+K`; Estoque, Ordens de Serviço, Fornecedores e Financeiro também têm campos de busca independentes. Relatórios pode ser filtrado por título e conteúdo na própria aba.

A identidade visual usa recortes localizados da logo exibida na referência. Em **Financeiro**, existe a conta local `Conta da Assistência · InfinitePay`; cada lançamento pode ser associado a uma conta, e os saldos são calculados usando o saldo inicial e receitas/despesas marcadas como pagas. Informe o saldo inicial real antes de usar esses valores para conciliação. Esta versão organiza os registros localmente; não acessa nem sincroniza com a conta online da InfinitePay.

## Iniciar no Windows 10/11

1. Instale Python 3.11 ou mais recente e habilite `py` no PATH.
2. Execute `iniciar_lmf.bat`. Na primeira execução o script cria `.venv`, instala os requisitos e inicia o servidor.
3. Acesse http://127.0.0.1:8000. Para encerrar, feche a janela do servidor.

No VS Code, abra a pasta raiz do projeto, selecione o interpretador `.venv` e inicie a configuração **LMF Control (FastAPI)** na aba Executar e Depurar. Não execute `app/main.py` diretamente: o comando correto é `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`, pois os módulos usam imports relativos ao pacote `app`.

No primeiro acesso, crie o usuário administrador. Na tela de login, **Lembre de mim** mantém a sessão por 30 dias; sem marcar, ela dura 12 horas. As APIs exigem sessão autenticada; ações de escrita também verificam CSRF. O servidor aceita conexões somente de loopback por padrão. Não habilite `LMF_ALLOW_REMOTE` sem configurar também a proteção de rede apropriada.

O aplicativo escuta apenas no endereço local. A importação Excel recebe o arquivo original sem alterá-lo; a tela de estoque permite exportar uma nova planilha. O mapeamento aceita os campos `name`, `sku`, `category`, `supplier`, `quantity`, `min_quantity`, `cost` e `price` vinculados aos cabeçalhos da planilha.

## Módulos presentes

Estoque com cadastro, movimentação transacional, bloqueio de saldo negativo, busca, importação e exportação; dados e histórico de clientes vinculados às operações; ordens de serviço numeradas; pedidos de clientes e compras; fornecedores; garantias; lançamentos financeiros; indicadores do dashboard; backup local e restauração validada.

O módulo **Vendas** registra venda com vários itens via API (`POST /api/sales`) e um item pela interface; a confirmação baixa estoque e registra somente o valor efetivamente recebido no financeiro, em uma transação. Use o cabeçalho `Idempotency-Key` para impedir repetição quando o cliente repetir uma solicitação. Relatórios refletem o banco e exportam vendas e estoque para Excel; a margem é estimada pelo custo de produto registrado e não inclui taxas, impostos ou despesas indiretas. Para API e documentação interativa, acesse `/docs`. A estrutura do JSON é versionada pelo campo `schema_revision`.


## Integração Supabase opcional (desativada por padrão)

O banco local JSON continua sendo a única fonte de dados até que a integração seja habilitada explicitamente. Não há conexão durante a inicialização do aplicativo, sincronização automática nem alteração do JSON quando as variáveis estão ausentes ou desativadas. A ponte envia um snapshot JSON completo para uma única linha `jsonb` em `public.lmf_json_state`; ela não converte as tabelas para o modelo relacional do Supabase.

Quando decidir preparar a integração:

1. Crie um projeto privado no Supabase e execute manualmente `supabase/lmf_json_state.sql` no SQL Editor.
2. Configure localmente `SUPABASE_URL`, `SUPABASE_SECRET_KEY` e `LMF_SUPABASE_SYNC_ENABLED=true` no ambiente do processo. Não coloque a secret key no frontend, no Git ou em `.env.example`; a chave dá acesso administrativo e fica somente no servidor local.
3. Rode `python scripts/supabase_sync.py status` para conferir a configuração sem fazer requisições.
4. Para enviar uma cópia local, rode `python scripts/supabase_sync.py push`. Isso só ocorre por comando explícito.
5. Para substituir o JSON local pela cópia remota, pare o servidor e rode `python scripts/supabase_sync.py pull --confirm-replace`. O comando valida o conteúdo e cria um backup local antes da substituição. Depois, reinicie o aplicativo.

**Estado atual (opção B: o JSON continua sendo o banco principal; o Supabase é um espelho).** Em 30/10/2026 a tabela `public.lmf_json_state` foi criada no projeto **LMF CONTROL** (`wcubsqxxqadodaynothx`) pela migração `lmf_json_state_mirror`, com RLS ligado e acesso apenas para `service_role` (`anon` e `authenticated` não leem nem gravam). A tabela está **vazia**: nenhum dado foi enviado. Para o primeiro envio falta apenas você criar uma *secret key* em Project Settings > API Keys e colocá-la **somente no seu `.env` local** (`SUPABASE_SECRET_KEY`, `LMF_SUPABASE_SYNC_ENABLED=true`); depois rode `python scripts/supabase_sync.py status` e `push`. O envio e a leitura foram testados contra um servidor falso local e o formato da linha foi conferido na tabela remota; o envio real com a chave ainda não foi executado.

O snapshot inclui os registros de usuários e hashes de senha/código de recuperação junto com os dados operacionais. Proteja o projeto e a secret key como dados administrativos. `pull` substitui o estado local inteiro; não combina alterações linha a linha. Resolva divergências antes de escolher qual lado enviar.

## Dados e backup

O banco fica em `%LOCALAPPDATA%\LMF-Control\data\lmf_control.json`; planilhas, documentos e configurações ficam em `%LOCALAPPDATA%\LMF-Control\data\`. Os backups JSON são salvos em `%USERPROFILE%\Documents\LMF-Control\Backups\`, e arquivos a importar devem ser colocados em `%USERPROFILE%\Documents\LMF-Control\Importar-Backups\`. Na primeira execução, um banco SQLite anterior é convertido para JSON e validado; arquivos legados de backup continuam importáveis. A planilha fornecida foi preservada na pasta `planilhas` dos dados persistentes. Seu importador reconhece automaticamente a aba `Estoque e Caixa`, o cabeçalho na linha 6 e os campos Produto, Categoria, Código/SKU, Fornecedor, Qtd. estoque, Custo unit. e Preço venda unit. As linhas de produtos desta cópia estão vazias; o sistema informa zero produtos e não cria dados de exemplo. Colunas de vendas e caixa da planilha são mantidas no arquivo de referência, mas não são lançadas automaticamente como receita ou movimentações, pois a planilha não informa datas ou confirmações de recebimento. Use **Backup e Restauração** para criar e validar cópias, abrir as pastas, importar um ZIP da pasta de importação, restaurar com confirmação e excluir cópias (o último backup íntegro é protegido). A tela mostra integridade, data, estado da última execução e permite configurar horário e retenção. O backup automático grava snapshots JSON e roda diariamente enquanto o processo estiver aberto. Cópias locais não protegem contra perda física do mesmo disco; copie backups periodicamente para uma unidade externa.

Para gerar uma cópia JSON sanitizada no VS Code, use **Terminal → Executar Tarefa... → LMF Control: Exportar banco para JSON**. O arquivo padrão é `outputs/LMF_Control_banco_dados.json`; a exportação é somente leitura e omite hashes de senha.

Se a pasta Documentos não puder ser criada, os backups passam a usar `%LOCALAPPDATA%\LMF-Control\Backups` e a interface informa o caminho alternativo. No módulo **Financeiro**, um administrador pode remover o saldo inicial de uma conta após revisar valor e data, confirmar o nome da conta e, quando existirem movimentações vinculadas, confirmar novamente. A operação grava uma correção administrativa e recalcula o saldo sem excluir nem alterar transações reais.

Para inicializar com Windows, crie um atalho de `iniciar_lmf.bat` na pasta `shell:startup`. A janela precisa permanecer em execução para os backups diários.

## Migração do JSON para PostgreSQL / Supabase (script; o app ainda usa o JSON)

`scripts/migrate_json_to_postgres.py` copia o banco JSON para tabelas relacionais (o mesmo esquema de `app/models.py`) em um PostgreSQL, inclusive o do Supabase. **Isso não troca o banco em uso pelo aplicativo**: o LMF Control continua lendo e gravando o JSON. O script serve para preparar, validar e carregar uma cópia relacional.

Garantias do script:

- o JSON de origem é aberto só para leitura e seu hash SHA-256 é conferido antes e depois;
- sem `--apply` nada é gravado no destino (simulação), e o relatório mostra o que seria inserido;
- só insere registros ausentes; nunca atualiza nem apaga. Linhas já existentes com conteúdo diferente são apenas relatadas;
- tudo roda em uma única transação (se algo falhar, nada é gravado);
- antes de gravar, uma cópia do JSON é salva, sem sobrescrever cópias anteriores (padrão: `data/backups/pre-migracao/`, ou `--backup-dir`);
- depois de gravar, relê o destino e confere cada linha; colunas ou tabelas do JSON que o esquema não comporta são avisadas;
- destinos `*.supabase.co`/`*.supabase.com` só recebem dados com `--apply --allow-remote`.

Passo a passo recomendado (sempre em um banco de teste ou cópia antes do real):

```
python scripts/migrate_json_to_postgres.py --json CAMINHO\lmf_control.json --database-url "postgresql+psycopg://usuario:senha@host:5432/banco"
python scripts/migrate_json_to_postgres.py --json CAMINHO\lmf_control.json --database-url "..." --apply --report relatorio.json
```

A URL também pode vir da variável `LMF_DATABASE_URL`. Código de saída: 0 = sucesso, 1 = erro (nada gravado), 2 = gravou, mas a validação encontrou diferenças. Valores monetários continuam como `Float` (precisão dupla), exatamente como no JSON; mudar para `NUMERIC` é uma decisão separada. Os testes (`tests/test_migration.py`) rodam em SQLite e, com `LMF_TEST_DATABASE_URL` apontando para um PostgreSQL **descartável**, também em PostgreSQL (o schema `public` desse banco é apagado a cada teste).

## Validação de telefone/CPF e fotos das ordens de serviço

- **Telefone e WhatsApp** (clientes e fornecedores): DDD + número, no máximo 11 dígitos (DDD + 9 dígitos). Letras são bloqueadas na digitação e recusadas pelo servidor. Também é aceito DDD + 8 dígitos (telefone fixo). Formato gravado: `(92) 9 9815-8767`.
- **CPF** (cadastro de clientes): exatamente 11 números, gravado como `999.888.777-54`. Só o formato é conferido; os dígitos verificadores não são calculados. CNPJ (14 dígitos) deixa de ser aceito nesse campo.
- Registros antigos fora do padrão continuam aparecendo nas listas; para editá-los é preciso corrigir o valor (o formulário avisa e não corta o dado em silêncio).
- **Fotos da OS**: até 6 por ordem de serviço, JPEG, PNG ou WebP, até 8 MB cada. O tipo é conferido pelo conteúdo do arquivo. Ficam em `data/documentos/ordens-servico/<id da OS>/`, pasta já incluída nos backups e devolvida na restauração. Excluir a OS exclui as fotos. A tabela `service_order_photos` (migração `20261029_0006`) guarda só os metadados; o script de migração para PostgreSQL copia essas linhas, **mas não os arquivos de imagem**.
- Avisos de erro ao salvar agora aparecem por cima do formulário aberto. Antes ficavam escondidos atrás dele, e a tela parecia travada.

## Testes

Após instalar os requisitos: `python -m pytest`. Para iniciar manualmente: `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`.

## Limites desta entrega

A tela de venda registra um produto por operação; a API aceita vários itens. O sistema mantém controle local da conta da InfinitePay, sem integração ou conciliação automática com o provedor. Backups JSON novos incluem manifesto e versão do esquema; arquivos corrompidos são recusados. Backups SQLite antigos continuam sendo lidos somente para conversão durante a restauração. Os relatórios exportam vendas e estoque; o desempenho dos fornecedores aparece na API de resumo. Análises detalhadas de compras, extratos bancários e taxas ainda não têm relatórios dedicados. O acompanhamento de ocorrência de garantia existe pela API, sem interface própria. O recebimento de compras tem endpoint transacional (`POST /api/orders/{id}/receive`) que atualiza estoque só no recebimento e exige `Idempotency-Key`; ainda não há tela própria para esse fluxo.
