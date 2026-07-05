from fastapi import FastAPI, Request, Form, Query, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from databases import Database
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import math
from datetime import datetime
from db import init_engine, fetch_one, fetch_all, execute
from openai import OpenAI

DATABASE_URL = "postgresql://cloudflowz:Fr33dumb123!@localhost:5444/cloudflowz_dev"
# Was "svx" -- pointed at the wrong schema for editing Playvocal content.
# get_crud_tables()/get_table_columns()/etc. all filter by this value, so
# nothing under playvocal.* would show up in the tool until this matched.
SCHEMA_NAME = "playvocal"

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory="templates")
database = Database(DATABASE_URL)

@app.on_event("startup")
async def startup():
    await database.connect()

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

@app.get("/health")
async def health_check():
    return {"status": "ok"}

# ----------------------------
# Helper functions
# ----------------------------
async def get_crud_tables():
    query = """
        SELECT t.table_name
        FROM information_schema.tables t
        LEFT JOIN pg_catalog.pg_description d
            ON d.objoid = (t.table_schema||'.'||t.table_name)::regclass
        WHERE t.table_schema = :schema
        AND COALESCE(d.description,'')='CRUD'
        AND t.table_type='BASE TABLE'
        ORDER BY t.table_name
    """
    rows = await database.fetch_all(query=query, values={"schema": SCHEMA_NAME})
    return [r[0] for r in rows]

async def get_table_columns(table_name: str):
    """
    Returns a list of tuples:
        (column_name, data_type, is_locked, character_maximum_length)
    """
    query = """
        SELECT
            column_name,
            data_type,
            character_maximum_length,
            CASE WHEN ordinal_position = 1 THEN TRUE ELSE FALSE END AS is_locked
        FROM information_schema.columns
        WHERE table_schema = :schema
          AND table_name = :table
        ORDER BY ordinal_position
    """
    rows = await database.fetch_all(query=query, values={"schema": SCHEMA_NAME, "table": table_name})
    return [(r['column_name'], r['data_type'], r['is_locked'], r['character_maximum_length']) for r in rows]

async def get_table_rows(
    table_name: str,
    page: int = 1,
    per_page: int = 20,
    sort: str | None = None,
    sort_dir: str = "asc"
):
    offset = (page - 1) * per_page

    # Treat "None" string as actual None
    if sort in ("", "None"):
        sort = None

    if sort:
        sort_dir = (sort_dir or "asc").lower()
        if sort_dir not in ("asc", "desc"):
            sort_dir = "asc"
        query = f'SELECT * FROM "{SCHEMA_NAME}"."{table_name}" ORDER BY "{sort}" {sort_dir} LIMIT :limit OFFSET :offset'
    else:
        query = f'SELECT * FROM "{SCHEMA_NAME}"."{table_name}" ORDER BY 1 LIMIT :limit OFFSET :offset'

    rows = await database.fetch_all(query=query, values={"limit": per_page, "offset": offset})
    return [dict(r) for r in rows]

async def get_total_rows(table_name: str):
    query = f'SELECT COUNT(*) FROM "{SCHEMA_NAME}"."{table_name}"'
    row = await database.fetch_one(query=query)
    return row[0]

# ----------------------------
# Required Column helper
# ----------------------------
async def get_required_columns(table_name: str):
    query = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = :schema
          AND table_name = :table
          AND is_nullable = 'NO'
          AND ordinal_position > 1
    """
    rows = await database.fetch_all(
        query=query,
        values={"schema": SCHEMA_NAME, "table": table_name}
    )
    return {r[0] for r in rows}

async def get_locked_columns(table: str):
    query = """
        SELECT
            c.column_name,
            c.ordinal_position,
            pgd.description
        FROM information_schema.columns c
        LEFT JOIN pg_catalog.pg_description pgd
            ON pgd.objoid = (c.table_schema||'.'||c.table_name)::regclass
           AND pgd.objsubid = c.ordinal_position
        WHERE c.table_schema = :schema
          AND c.table_name = :table
        ORDER BY c.ordinal_position
    """
    rows = await database.fetch_all(
        query=query,
        values={"schema": SCHEMA_NAME, "table": table}
    )

    columns = [r["column_name"] for r in rows]

    locked_columns = {
        r["column_name"]: (
            r["ordinal_position"] == 1 or
            (r["description"] or "").strip().upper() == "LOCKED"
        )
        for r in rows
    }

    return columns, locked_columns

# ----------------------------
# Render table helper
# ----------------------------
async def render_table(
    request: Request,
    selected_table: str,
    page: int = 1,
    per_page: int = 20,
    sort: str | None = None,
    sort_dir: str = "asc",
    selected_row_id: int | None = None,
    edit_mode: bool = False
):
    columns_raw = await get_table_columns(selected_table)
    columns = [col[0] for col in columns_raw]
    data_types = {col[0]: col[1] for col in columns_raw}

    rows = await get_table_rows(selected_table, page=page, per_page=per_page, sort=sort, sort_dir=sort_dir)
    total_rows = await get_total_rows(selected_table)
    total_pages = max(1, math.ceil(total_rows / per_page))

    return templates.TemplateResponse("crud_content.html", {
        "request": request,
        "columns": columns,
        "data_types": data_types,
        "rows": rows,
        "selected_table": selected_table,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "sorted_column": sort,
        "sort_dir": sort_dir,
        "selected_row_id": selected_row_id,
        "edit_mode": edit_mode
    })

# ----------------------------
# Type casting for edits
# ----------------------------
def cast_value(value: str, data_type: str):
    """Convert a string from form input to the correct Python type for Postgres."""
    if value is None:
        return None

    data_type = data_type.lower()

    # Boolean
    if data_type in ("boolean", "bool"):
        return str(value).lower() in ("true", "1", "t", "on")

    # Integers
    if data_type in ("integer", "int", "smallint", "bigint"):
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

    # Floats / numerics
    if data_type in ("numeric", "real", "double precision", "decimal"):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    # Date / timestamp
    if data_type in (
        "timestamp without time zone",
        "timestamp with time zone",
        "date"
    ):
        try:
            # Try parsing full datetime with microseconds
            dt = datetime.fromisoformat(value)
            # If column is just 'date', return date part only
            if data_type == "date":
                return dt.date()
            return dt
        except ValueError:
            # fallback: try date-only format
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                return None

    # Default: leave as string
    return value

# ----------------------------
# Routes
# ----------------------------
@app.get("/", response_class=HTMLResponse)
async def crud_index(request: Request):
    tables = await get_crud_tables()
    if not tables:
        return HTMLResponse("No CRUD tables found in schema.")
    
    selected_table = tables[0]

    # Get total rows and pages for this table
    total_rows = await get_total_rows(selected_table)
    per_page = 20
    total_pages = max(1, math.ceil(total_rows / per_page))

    return templates.TemplateResponse("crud_swap.html", {
        "request": request,
        "tables": tables,
        "selected_table": selected_table,
        "schema": SCHEMA_NAME,
        "page": 1,
        "per_page": per_page,
        "total_pages": total_pages,
        "sorted_column": None,
        "sort_dir": "asc"
    })

@app.get("/crud_table", response_class=HTMLResponse)
async def crud_table(
    request: Request,
    selected_table: str = Query(...),
    page: int = Query(1),
    per_page: int = Query(20),
    sort: str | None = Query(None),
    sort_dir: str | None = Query(None),
    selected_row_id: int | None = Query(None)  # NEW
):
    # Toggle sort direction
    if not sort_dir:
        sort_dir = "asc"
    elif sort_dir.lower() == "asc":
        sort_dir = "desc"
    else:
        sort_dir = "asc"

    return await render_table(
        request,
        selected_table,
        page,
        per_page,
        sort,
        sort_dir=sort_dir,
        selected_row_id=selected_row_id  # pass to template
    )

@app.get("/crud", response_class=HTMLResponse)
async def crud_alias(request: Request):
    return await crud_index(request)

# ----------------------------
# Add row
# ----------------------------
@app.post("/crud/add", response_class=HTMLResponse)
async def add_row(request: Request, table: str = Form(...), **kwargs):
    columns_raw = await get_table_columns(table)
    columns = [col[0] for col in columns_raw]
    data_types = {col[0]: col[1] for col in columns_raw}
    insert_cols = columns[1:]  # skip PK
    insert_vals = {k: cast_value(v, data_types[k]) for k, v in kwargs.items() if k in insert_cols}
    if insert_cols:
        col_names = ','.join(f'"{c}"' for c in insert_cols)
        val_names = ','.join(f':{c}' for c in insert_cols)
        query = f'INSERT INTO "{SCHEMA_NAME}"."{table}" ({col_names}) VALUES ({val_names})'
        await database.execute(query=query, values=insert_vals)
    return await render_table(request, selected_table=table)

# ----------------------------
# Delete row
# ----------------------------
@app.post("/crud/delete", response_class=HTMLResponse)
async def delete_row(request: Request, table: str = Form(...), id: int = Form(...)):
    columns_raw = await get_table_columns(table)
    if not columns_raw:
        return HTMLResponse("Table not found or has no columns", status_code=400)
    pk_column = columns_raw[0][0]
    try:
        await database.execute(
            f'DELETE FROM "{SCHEMA_NAME}"."{table}" WHERE "{pk_column}" = :id',
            values={"id": id}
        )
    except Exception as e:
        return HTMLResponse(f"Error deleting row: {str(e)}", status_code=500)
    return await render_table(request, selected_table=table, page=1, per_page=20)

# ----------------------------
# Inline edit form
# ----------------------------
@app.get("/crud_edit_form", response_class=HTMLResponse)
async def edit_form(
    request: Request,
    table: str = Query(...),
    id: int = Query(...)
):
    try:
        query = """
            SELECT
                c.column_name,
                c.data_type,
                c.character_maximum_length,
                pgd.description AS comment
            FROM information_schema.columns c
            LEFT JOIN pg_catalog.pg_description pgd
                ON pgd.objoid = (c.table_schema||'.'||c.table_name)::regclass
               AND pgd.objsubid = c.ordinal_position
            WHERE c.table_schema = :schema
              AND c.table_name = :table
            ORDER BY c.ordinal_position
        """
        rows = await database.fetch_all(
            query=query,
            values={"schema": SCHEMA_NAME, "table": table}
        )

        if not rows:
            return HTMLResponse("Table has no columns", status_code=400)

        columns = [r["column_name"] for r in rows]
        data_types = {r["column_name"]: r["data_type"] for r in rows}
        column_lengths = {r["column_name"]: r["character_maximum_length"] for r in rows}  # NEW

        # ✅ LOCKED columns (PK or comment=LOCKED)
        locked_columns = {
            r["column_name"]: (
                r["column_name"] == columns[0] or (r["comment"] or "").upper() == "LOCKED"
            )
            for r in rows
        }

        required_columns = await get_required_columns(table)

        pk_column = columns[0]
        row = await database.fetch_one(
            f'SELECT * FROM "{SCHEMA_NAME}"."{table}" WHERE "{pk_column}" = :id',
            {"id": id}
        )

        if not row:
            return HTMLResponse("Row not found", status_code=404)

        return templates.TemplateResponse(
            "crud_edit_form.html",
            {
                "request": request,
                "row": dict(row),
                "columns": columns,
                "selected_table": table,
                "data_types": data_types,
                "column_lengths": column_lengths,  # PASS LENGTHS
                "locked_columns": locked_columns,
                "required_columns": required_columns,
                "insert_mode": False
            }
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        return HTMLResponse(str(e), status_code=500)

# ----------------------------
# Save edited row
# ----------------------------
@app.post("/crud/edit", response_class=HTMLResponse)
async def edit_row(request: Request):
    form = await request.form()
    table = form.get("table")
    id_val = form.get("id")

    if not table or not id_val:
        return HTMLResponse("Missing table or ID", status_code=400)

    try:
        id_int = int(id_val)
    except ValueError:
        return HTMLResponse("Invalid ID", status_code=400)

    # Column metadata
    columns_raw = await get_table_columns(table)
    columns = [c[0] for c in columns_raw]
    data_types = {c[0]: c[1] for c in columns_raw}

    # Get locked columns from comments and PK
    query = """
        SELECT column_name, pgd.description
        FROM information_schema.columns c
        LEFT JOIN pg_catalog.pg_description pgd
            ON pgd.objoid = (c.table_schema||'.'||c.table_name)::regclass
           AND pgd.objsubid = c.ordinal_position
        WHERE c.table_schema = :schema
          AND c.table_name = :table
    """
    rows = await database.fetch_all(query=query, values={"schema": SCHEMA_NAME, "table": table})
    locked_columns = {r["column_name"]: (r["column_name"] == columns[0] or r["description"] == "LOCKED") for r in rows}

    # NOT NULL columns
    required_columns = await get_required_columns(table)

    # Only update editable columns that exist in the form and are NOT locked
    update_cols = [c for c in columns if c in form and not locked_columns.get(c, False)]
    values = {}

    for c in update_cols:
        raw = form.get(c)
        if c in required_columns and str(raw).strip() == "":
            return HTMLResponse(f"Column '{c}' cannot be empty", status_code=400)
        values[c] = cast_value(raw, data_types[c])

    values["id"] = id_int

    if update_cols:
        set_clause = ", ".join(f'"{c}" = :{c}' for c in update_cols)
        await database.execute(
            f'UPDATE "{SCHEMA_NAME}"."{table}" SET {set_clause} WHERE "{columns[0]}" = :id',
            values=values
        )

    return await render_table(request, selected_table=table, selected_row_id=id_int)

# ----------------------------
# Inline add form (INSERT)
# ----------------------------
@app.get("/crud_add_form", response_class=HTMLResponse)
async def crud_add_form(request: Request, selected_table: str):
    columns_raw = await get_table_columns(selected_table)
    columns = [c[0] for c in columns_raw]
    data_types = {c[0]: c[1] for c in columns_raw}
    column_lengths = {c[0]: c[3] for c in columns_raw}  # NEW

    # ✅ PK ALWAYS LOCKED
    locked_columns = {c: (c == columns[0] or c in ["updated_by", "updated_dt"]) for c in columns}

    required_columns = await get_required_columns(selected_table)

    show_columns = [c for c in columns if not locked_columns[c]]
    empty_row = {c: "" for c in show_columns}

    return templates.TemplateResponse(
        "crud_edit_form.html",
        {
            "request": request,
            "columns": show_columns,
            "data_types": data_types,
            "column_lengths": column_lengths,  # PASS LENGTHS
            "locked_columns": locked_columns,
            "required_columns": required_columns,
            "row": empty_row,
            "selected_table": selected_table,
            "insert_mode": True
        }
    )

@app.post("/crud/insert", response_class=HTMLResponse)
async def insert_row(request: Request):
    form = await request.form()
    table = form.get("table")

    if not table:
        return HTMLResponse("Missing table", status_code=400)

    columns_raw = await get_table_columns(table)
    columns = [c[0] for c in columns_raw]
    data_types = {c[0]: c[1] for c in columns_raw}
    locked_columns = {c[0]: (c[0] == columns[0]) for c in columns_raw}
    required_columns = await get_required_columns(table)

    insert_cols = [c for c in columns if c in form and not locked_columns[c]]
    values = {}

    for c in insert_cols:
        raw = form[c]
        if c in required_columns and str(raw).strip() == "":
            return HTMLResponse(
                f"Column '{c}' cannot be empty",
                status_code=400
            )
        values[c] = cast_value(raw, data_types[c])

    try:
        if insert_cols:
            col_names = ",".join(f'"{c}"' for c in insert_cols)
            val_names = ",".join(f":{c}" for c in insert_cols)
            query = f'''
                INSERT INTO "{SCHEMA_NAME}"."{table}"
                ({col_names})
                VALUES ({val_names})
            '''
            await database.execute(query=query, values=values)
    except Exception as e:
        return HTMLResponse(str(e), status_code=500)

    return await render_table(request, selected_table=table)

@app.get("/menu", response_class=HTMLResponse)
async def menu_home(request: Request):
    print("MENU ROUTE HIT")
    return templates.TemplateResponse(
        "menu_base.html",
        {"request": request}
    )

@app.get("/menu/crud", response_class=HTMLResponse)
async def menu_crud(request: Request):
    return await crud_index(request)

@app.get("/menu/multiple_choice", response_class=HTMLResponse)
async def multiple_choice(request: Request):
    # Initially just render the placeholder page with the dropdown and grid container
    return templates.TemplateResponse(
        "multiple_choice.html",
        {"request": request}
    )

@app.get("/menu/story", response_class=HTMLResponse)
async def call_detail(request: Request):
    return templates.TemplateResponse(
        "story.html",
        {"request": request}
    )

#
#  TWILIO Requirements 
#
@app.post("/twilio/fallback")
async def twilio_fallback():
    return Response(
        """
        <Response>
            <Say voice="Polly.Kendra-Neural" language="en-US">
                The system is unavailable. Please try again when you like.
            </Say>
            <Hangup/>
        </Response>
        """,
        media_type="application/xml",
    )

