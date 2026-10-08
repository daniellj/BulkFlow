/*=============================================================================
  ATIVAÇÃO DE CDC EM LISTA DE TABELAS — EXECUÇÃO LOCAL OU VIA LINKED SERVER

  Revisão: execução remota por RPC e verificação final das tabelas CDC

  Altere somente os parâmetros de entrada abaixo.

  @LinkedServerName = NULL          -> executa na instância local
  @LinkedServerName = N'LINKED_X'   -> executa na instância remota
=============================================================================*/

SET NOCOUNT ON;

-- =============================================================================
-- PARÂMETROS DE ENTRADA
-- =============================================================================
DECLARE
    @LinkedServerName SYSNAME       = NULL,
    @DatabaseName     SYSNAME       = N'<SOURCE_DATABASE_NAME>',
    @SchemaName       SYSNAME       = N'<SOURCE_SCHEMA_NAME>',
    @TableListJson    NVARCHAR(MAX) = N'["<TABLE_NAME_01>", "<TABLE_NAME_02>", "<TABLE_NAME_03>"]';

-- =============================================================================
-- VARIÁVEIS INTERNAS — NÃO É NECESSÁRIO ALTERAR
-- =============================================================================
DECLARE
    @TableList           NVARCHAR(MAX),
    @ProcedureDefinition NVARCHAR(MAX),
    @TargetCommand       NVARCHAR(MAX),
    @SqlCommand          NVARCHAR(MAX),
    @ErrorMessage        NVARCHAR(4000),
    @ErrorSeverity       INT,
    @ErrorState          INT;

BEGIN TRY
    -----------------------------------------------------------------------------
    -- Normalização e validações dos parâmetros de entrada
    -----------------------------------------------------------------------------
    SET @LinkedServerName = NULLIF(LTRIM(RTRIM(@LinkedServerName)), N'');

    IF NULLIF(LTRIM(RTRIM(@DatabaseName)), N'') IS NULL
        RAISERROR(N'O nome do banco de dados deve ser informado.', 16, 1);

    IF @LinkedServerName IS NULL
    BEGIN
        IF DB_ID(@DatabaseName) IS NULL
            RAISERROR(N'O banco de dados informado não existe ou não está acessível na instância local.', 16, 1);
    END
    ELSE
    BEGIN
        IF NOT EXISTS
        (
            SELECT 1
            FROM sys.servers
            WHERE [name] = @LinkedServerName
              AND is_linked = 1
        )
            RAISERROR(N'O Linked Server informado não existe na instância local.', 16, 1);

        IF EXISTS
        (
            SELECT 1
            FROM sys.servers
            WHERE [name] = @LinkedServerName
              AND is_linked = 1
              AND is_rpc_out_enabled = 0
        )
            RAISERROR(N'O Linked Server informado está com RPC OUT desabilitado.', 16, 1);
    END;

    IF NULLIF(LTRIM(RTRIM(@SchemaName)), N'') IS NULL
        RAISERROR(N'O nome do esquema deve ser informado.', 16, 1);

    IF ISJSON(@TableListJson) <> 1
        RAISERROR(N'A lista de tabelas deve ser informada como um array JSON válido.', 16, 1);

    IF LEFT(LTRIM(@TableListJson), 1) <> N'['
        RAISERROR(N'A lista de tabelas deve ser informada como um array JSON.', 16, 1);

    IF EXISTS
    (
        SELECT 1
        FROM OPENJSON(@TableListJson)
        WHERE [type] <> 1
           OR NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(256), [value]))), N'') IS NULL
    )
        RAISERROR(N'O array JSON deve conter somente nomes de tabelas válidos em formato texto.', 16, 1);

    -----------------------------------------------------------------------------
    -- Converte o array JSON para a lista separada por vírgulas já utilizada
    -- pelo procedimento validado, sem alterar sua lógica de ativação do CDC.
    -----------------------------------------------------------------------------
    SELECT @TableList = STUFF
    (
        (
            SELECT
                N',' + LTRIM(RTRIM(CONVERT(NVARCHAR(256), json_item.[value])))
            FROM OPENJSON(@TableListJson) AS json_item
            ORDER BY CONVERT(INT, json_item.[key])
            FOR XML PATH(N''), TYPE
        ).value(N'.', N'NVARCHAR(MAX)'),
        1,
        1,
        N''
    );

    IF NULLIF(@TableList, N'') IS NULL
        RAISERROR(N'A lista JSON deve conter ao menos uma tabela.', 16, 1);

    -----------------------------------------------------------------------------
    -- Definição do procedimento temporário.
    -- A lógica abaixo foi mantida conforme o script original validado.
    -----------------------------------------------------------------------------
    SET @ProcedureDefinition = N'
CREATE PROCEDURE dbo.sp_tmp_enable_cdc_tables
    @source_schema SYSNAME,
    @table_list NVARCHAR(MAX)
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE 
        @table_name SYSNAME,
        @source_object_id INT,
        @supports_net_changes BIT,
        @position INT,
        @table_item NVARCHAR(256);

    -- Lista das tabelas que deseja habilitar CDC
    DECLARE @Tables TABLE (table_name SYSNAME);

    -- Split manual da lista, compatível com versões antigas do SQL Server
    SET @table_list = @table_list + N'','';

    WHILE CHARINDEX(N'','', @table_list) > 0
    BEGIN
        SET @position = CHARINDEX(N'','', @table_list);

        SET @table_item = LTRIM(RTRIM(SUBSTRING(@table_list, 1, @position - 1)));

        IF @table_item <> N''''
        BEGIN
            INSERT INTO @Tables (table_name)
            VALUES (@table_item);
        END;

        SET @table_list = SUBSTRING(@table_list, @position + 1, LEN(@table_list));
    END;

    -- Loop sobre cada tabela
    DECLARE table_cursor CURSOR FOR
        SELECT table_name FROM @Tables;

    OPEN table_cursor;
    FETCH NEXT FROM table_cursor INTO @table_name;

    WHILE @@FETCH_STATUS = 0
    BEGIN
        SET @source_object_id = OBJECT_ID(QUOTENAME(@source_schema) + N''.'' + QUOTENAME(@table_name), ''U'');

        IF @source_object_id IS NULL
        BEGIN
            PRINT ''Tabela não encontrada: '' + @source_schema + ''.'' + @table_name;
        END
        ELSE
        BEGIN
            -- Verifica se a tabela possui Primary Key
            IF EXISTS (
                SELECT 1
                FROM sys.key_constraints AS key_constraint
                WHERE key_constraint.parent_object_id = @source_object_id
                  AND key_constraint.[type] = ''PK''
            )
            BEGIN
                SET @supports_net_changes = 1;
            END
            ELSE
            BEGIN
                SET @supports_net_changes = 0;
            END;

            IF NOT EXISTS (
                SELECT 1
                FROM cdc.change_tables
                WHERE source_object_id = @source_object_id
            )
            BEGIN
                PRINT ''Habilitando CDC para a tabela: ''
                    + @source_schema + ''.'' + @table_name
                    + '' | supports_net_changes = ''
                    + CAST(@supports_net_changes AS VARCHAR(1));

                EXEC sys.sp_cdc_enable_table
                    @source_schema        = @source_schema,
                    @source_name          = @table_name,
                    @role_name            = NULL,
                    @supports_net_changes = @supports_net_changes;
            END
            ELSE
            BEGIN
                PRINT ''CDC já habilitado para a tabela: ''
                    + @source_schema + ''.'' + @table_name;
            END;
        END;

        FETCH NEXT FROM table_cursor INTO @table_name;
    END;

    CLOSE table_cursor;
    DEALLOCATE table_cursor;
END;';

    -----------------------------------------------------------------------------
    -- Cria o procedimento no banco de destino.
    -- Quando @LinkedServerName for NULL, executa localmente.
    -- Quando informado, envia o mesmo comando para a instância remota.
    -----------------------------------------------------------------------------
    SET @TargetCommand = N'
IF DB_ID(N''' + REPLACE(@DatabaseName, N'''', N'''''') + N''') IS NULL
    RAISERROR(N''O banco de dados informado não existe ou não está acessível na instância de destino.'', 16, 1);

USE ' + QUOTENAME(@DatabaseName) + N';

IF OBJECT_ID(N''dbo.sp_tmp_enable_cdc_tables'', N''P'') IS NOT NULL
    DROP PROCEDURE dbo.sp_tmp_enable_cdc_tables;

EXEC sys.sp_executesql N''' + REPLACE(@ProcedureDefinition, N'''', N'''''') + N''';';

    IF @LinkedServerName IS NULL
    BEGIN
        EXEC sys.sp_executesql @TargetCommand;
    END
    ELSE
    BEGIN
        -- Executa o lote na instância remota por RPC, chamando o
        -- sp_executesql do banco master do Linked Server.
        SET @SqlCommand = N'EXEC '
                        + QUOTENAME(@LinkedServerName)
                        + N'.master.dbo.sp_executesql @RemoteCommand;';

        EXEC sys.sp_executesql
            @SqlCommand,
            N'@RemoteCommand NVARCHAR(MAX)',
            @RemoteCommand = @TargetCommand;
    END;

    -----------------------------------------------------------------------------
    -- Executa o procedimento com o esquema e a lista definidos no topo.
    -----------------------------------------------------------------------------
    SET @TargetCommand = N'
USE ' + QUOTENAME(@DatabaseName) + N';

EXEC dbo.sp_tmp_enable_cdc_tables
    @source_schema = N''' + REPLACE(@SchemaName, N'''', N'''''') + N''',
    @table_list    = N''' + REPLACE(@TableList, N'''', N'''''') + N''';';

    IF @LinkedServerName IS NULL
    BEGIN
        EXEC sys.sp_executesql @TargetCommand;
    END
    ELSE
    BEGIN
        -- Executa o lote na instância remota por RPC, chamando o
        -- sp_executesql do banco master do Linked Server.
        SET @SqlCommand = N'EXEC '
                        + QUOTENAME(@LinkedServerName)
                        + N'.master.dbo.sp_executesql @RemoteCommand;';

        EXEC sys.sp_executesql
            @SqlCommand,
            N'@RemoteCommand NVARCHAR(MAX)',
            @RemoteCommand = @TargetCommand;
    END;

    -----------------------------------------------------------------------------
    -- Verifica o resultado da ativação do CDC usando os mesmos parâmetros
    -- definidos no topo do script. A consulta é executada no mesmo destino:
    -- instância local quando @LinkedServerName for NULL ou instância remota
    -- quando um Linked Server for informado.
    -----------------------------------------------------------------------------
    SET @TargetCommand = N'
USE ' + QUOTENAME(@DatabaseName) + N';

DECLARE
    @source_schema  SYSNAME       = N''' + REPLACE(@SchemaName, N'''', N'''''') + N''',
    @table_list_json NVARCHAR(MAX) = N''' + REPLACE(@TableListJson, N'''', N'''''') + N''';

;WITH requested_tables AS
(
    SELECT
        sort_order = CONVERT(INT, json_item.[key]),
        source_table = UPPER
        (
            LTRIM(RTRIM(CONVERT(NVARCHAR(256), json_item.[value])))
        ),
        capture_instance = LOWER
        (
            CONCAT
            (
                @source_schema,
                N''_'',
                LTRIM(RTRIM(CONVERT(NVARCHAR(256), json_item.[value])))
            )
        )
    FROM OPENJSON(@table_list_json) AS json_item
)
SELECT
    requested_table.source_table,
    requested_table.capture_instance,
    CASE
        WHEN change_table.capture_instance IS NOT NULL THEN N''active''
        ELSE N''inactive''
    END AS cdc_status
FROM requested_tables AS requested_table
LEFT JOIN cdc.change_tables AS change_table
    ON change_table.capture_instance = requested_table.capture_instance
ORDER BY
    requested_table.source_table;';

    IF @LinkedServerName IS NULL
    BEGIN
        EXEC sys.sp_executesql @TargetCommand;
    END
    ELSE
    BEGIN
        -- Executa a verificação na instância remota pelo mesmo canal RPC
        -- utilizado na ativação das tabelas.
        SET @SqlCommand = N'EXEC '
                        + QUOTENAME(@LinkedServerName)
                        + N'.master.dbo.sp_executesql @RemoteCommand;';

        EXEC sys.sp_executesql
            @SqlCommand,
            N'@RemoteCommand NVARCHAR(MAX)',
            @RemoteCommand = @TargetCommand;
    END;
END TRY
BEGIN CATCH
    SELECT
        @ErrorMessage  = ERROR_MESSAGE(),
        @ErrorSeverity = ERROR_SEVERITY(),
        @ErrorState    = ERROR_STATE();

    -----------------------------------------------------------------------------
    -- Remove o procedimento temporário no destino mesmo quando ocorrer erro.
    -----------------------------------------------------------------------------
    BEGIN TRY
        SET @TargetCommand = N'
IF DB_ID(N''' + REPLACE(@DatabaseName, N'''', N'''''') + N''') IS NOT NULL
BEGIN
    USE ' + QUOTENAME(@DatabaseName) + N';

    IF OBJECT_ID(N''dbo.sp_tmp_enable_cdc_tables'', N''P'') IS NOT NULL
        DROP PROCEDURE dbo.sp_tmp_enable_cdc_tables;
END;';

        IF @LinkedServerName IS NULL
        BEGIN
            EXEC sys.sp_executesql @TargetCommand;
        END
        ELSE IF EXISTS
        (
            SELECT 1
            FROM sys.servers
            WHERE [name] = @LinkedServerName
              AND is_linked = 1
              AND is_rpc_out_enabled = 1
        )
        BEGIN
            -- Executa o lote na instância remota por RPC, chamando o
            -- sp_executesql do banco master do Linked Server.
            SET @SqlCommand = N'EXEC '
                            + QUOTENAME(@LinkedServerName)
                            + N'.master.dbo.sp_executesql @RemoteCommand;';

            EXEC sys.sp_executesql
                @SqlCommand,
                N'@RemoteCommand NVARCHAR(MAX)',
                @RemoteCommand = @TargetCommand;
        END;
    END TRY
    BEGIN CATCH
        -- Preserva o erro original da execução.
    END CATCH;

    -- Repropaga o erro original, preservando número, mensagem,
    -- procedimento e linha em que a falha ocorreu.
    THROW;
END CATCH;

-- Remove o procedimento temporário após a execução bem-sucedida.
SET @TargetCommand = N'
USE ' + QUOTENAME(@DatabaseName) + N';

IF OBJECT_ID(N''dbo.sp_tmp_enable_cdc_tables'', N''P'') IS NOT NULL
    DROP PROCEDURE dbo.sp_tmp_enable_cdc_tables;';

IF @LinkedServerName IS NULL
BEGIN
    EXEC sys.sp_executesql @TargetCommand;
END
ELSE
BEGIN
    -- Executa o lote na instância remota por RPC, chamando o
    -- sp_executesql do banco master do Linked Server.
    SET @SqlCommand = N'EXEC '
                    + QUOTENAME(@LinkedServerName)
                    + N'.master.dbo.sp_executesql @RemoteCommand;';

    EXEC sys.sp_executesql
        @SqlCommand,
        N'@RemoteCommand NVARCHAR(MAX)',
        @RemoteCommand = @TargetCommand;
END;
