# Version Locations Reference

## Current Version
- **Version:** 1.0.2
- **Name:** Kusha
- **Twin App:** Lava (SMS Web App)

---

## Files to Update

### 1. `main.py` (Primary Source)
```python
VERSION = "1.0.2"
CODENAME = "Kusha"
```

### 2. `README.md` (Header)
```markdown
# Kusha v1.0.2
```

### 3. `Dockerfile` (Label)
```dockerfile
LABEL version="1.0.2"
```

### 4. `docker-compose.yml` (Image Tag)
```yaml
image: kusha:1.0.2
```

---

## Quick Update Checklist

- [ ] `main.py` - VERSION constant
- [ ] `README.md` - Header
- [ ] `Dockerfile` - LABEL version
- [ ] `docker-compose.yml` - image tag

---

## Naming Convention

| Type | Named After | Examples |
|------|-------------|----------|
| **MAJOR** | People/Mythology | Kusha, Rama, Hanuman |
| **MINOR** | Places | Delhi, Ayodhya, Varanasi |
| **PATCH** | Things | Arrow, Bow, Lotus |

## Twin Apps

- **Kusha** - SMS Gateway API (this app)
- **Lava** - SMS Web App

Named after the twin sons of Rama in Hindu mythology.
