"""
Скрипт быстрого запуска сервиса диспетчеризации Ростелеком.
Запускает FastAPI сервер и веб-интерфейс диспетчера на http://localhost:8085
"""
import uvicorn

if __name__ == "__main__":
    print("=" * 60)
    print("  Сервис умной диспетчеризации выездных бригад Ростелеком")
    print("  Веб-интерфейс диспетчера доступен по адресу: http://localhost:8085")
    print("  Интерактивная документация Swagger:          http://localhost:8085/docs")
    print("=" * 60)
    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=8085, reload=True)
