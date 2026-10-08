USE [master];
GO

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

DECLARE
      @DatabaseName SYSNAME = N'<SOURCE_DATABASE_NAME>'
    , @SqlCommand NVARCHAR(MAX)
    , @ErrorMessage NVARCHAR(4000)
    , @DatabaseState NVARCHAR(60)
    , @IsCdcEnabled BIT
    , @CdcSchemaExists BIT = 0;

/* ============================================================================
   1. VALIDAÇÃO DO BANCO DE DADOS
   ============================================================================ */

SELECT
      @DatabaseState = state_desc
    , @IsCdcEnabled = is_cdc_enabled
FROM sys.databases
WHERE name = @DatabaseName;

IF @DatabaseState IS NULL
BEGIN
    SET @ErrorMessage =
          N'Banco de dados não encontrado: '
        + QUOTENAME(@DatabaseName)
        + N'.';

    THROW 51000, @ErrorMessage, 1;
END;

IF @DatabaseState <> N'ONLINE'
BEGIN
    SET @ErrorMessage =
          N'O banco de dados '
        + QUOTENAME(@DatabaseName)
        + N' não está ONLINE. Estado atual: '
        + @DatabaseState
        + N'.';

    THROW 51000, @ErrorMessage, 1;
END;

BEGIN TRY

    /* ========================================================================
       2. ATIVAÇÃO DO CDC NO BANCO DE DADOS
       ======================================================================== */

    IF @IsCdcEnabled = 0
    BEGIN
        RAISERROR(
            'Ativando o CDC no banco de dados %s.',
            0,
            1,
            @DatabaseName
        ) WITH NOWAIT;

        SET @SqlCommand =
              N'USE '
            + QUOTENAME(@DatabaseName)
            + N'; EXEC sys.sp_cdc_enable_db;';

        EXEC sys.sp_executesql @SqlCommand;
    END
    ELSE
    BEGIN
        RAISERROR(
            'O CDC já está habilitado no banco de dados %s.',
            0,
            1,
            @DatabaseName
        ) WITH NOWAIT;
    END;

    /* ========================================================================
       3. VALIDAÇÃO FINAL
       ======================================================================== */

    SELECT
          @DatabaseState = state_desc
        , @IsCdcEnabled = is_cdc_enabled
    FROM sys.databases
    WHERE name = @DatabaseName;

    SET @SqlCommand =
          N'USE '
        + QUOTENAME(@DatabaseName)
        + N';
          SELECT @SchemaExists =
              CASE
                  WHEN SCHEMA_ID(N''cdc'') IS NOT NULL THEN 1
                  ELSE 0
              END;';

    EXEC sys.sp_executesql
          @SqlCommand
        , N'@SchemaExists BIT OUTPUT'
        , @SchemaExists = @CdcSchemaExists OUTPUT;

    IF @IsCdcEnabled <> 1 OR @CdcSchemaExists <> 1
    BEGIN
        SET @ErrorMessage =
              N'A ativação do CDC não foi confirmada para o banco '
            + QUOTENAME(@DatabaseName)
            + N'.';

        THROW 51000, @ErrorMessage, 1;
    END;

    RAISERROR(
        'CDC habilitado e esquema cdc confirmado no banco de dados %s.',
        0,
        1,
        @DatabaseName
    ) WITH NOWAIT;

    SELECT
          database_name = @DatabaseName
        , database_state = @DatabaseState
        , is_cdc_enabled = @IsCdcEnabled
        , cdc_schema_exists = @CdcSchemaExists;

END TRY
BEGIN CATCH

    SET @ErrorMessage = LEFT(ERROR_MESSAGE(), 4000);

    RAISERROR(
        'Falha ao ativar o CDC no banco de dados %s: %s',
        10,
        1,
        @DatabaseName,
        @ErrorMessage
    ) WITH NOWAIT;

    THROW;

END CATCH;
GO
