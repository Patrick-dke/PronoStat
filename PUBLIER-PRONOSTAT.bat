@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Publication de PronoStat sur GitHub et Streamlit Cloud

echo ============================================================
echo   PUBLICATION DE PRONOSTAT
echo ============================================================
echo.
echo   Ce script va :
echo     1. Reparer et preparer le depot git local
echo     2. Ouvrir le formulaire GitHub pre-rempli
echo     3. Envoyer votre code sur GitHub
echo     4. Ouvrir Streamlit Cloud pour le deploiement
echo.
echo   Vous n'aurez qu'a valider dans le navigateur.
echo.
pause
echo.

REM ---------------------------------------------------------------
REM  ETAPE 0 : verifier que git est installe
REM ---------------------------------------------------------------
where git >nul 2>&1
if errorlevel 1 (
  echo [ERREUR] git n'est pas installe ou pas dans le PATH.
  echo Installez-le depuis https://git-scm.com/download/win puis relancez ce script.
  echo.
  pause
  exit /b 1
)
echo [OK] git detecte.

REM ---------------------------------------------------------------
REM  ETAPE 1 : nettoyer les verrous laisses par une operation avortee
REM ---------------------------------------------------------------
echo.
echo --- Nettoyage du depot ---
if exist ".git\index.lock" del /f /q ".git\index.lock" >nul 2>&1
if exist ".git\HEAD.lock" del /f /q ".git\HEAD.lock" >nul 2>&1
if exist ".git\packed-refs.lock" del /f /q ".git\packed-refs.lock" >nul 2>&1
if exist ".git\objects\maintenance.lock" del /f /q ".git\objects\maintenance.lock" >nul 2>&1
if exist ".git-rewrite" rmdir /s /q ".git-rewrite" >nul 2>&1
if exist "_tmp_test.txt" del /f /q "_tmp_test.txt" >nul 2>&1
if exist ".git\refs\original" rmdir /s /q ".git\refs\original" >nul 2>&1
if exist "PronoStat\.git" rmdir /s /q "PronoStat\.git" >nul 2>&1
if exist "PronoStat" rmdir "PronoStat" >nul 2>&1
echo [OK] Verrous et dossiers parasites supprimes.

REM ---------------------------------------------------------------
REM  ETAPE 2 : identite des commits
REM ---------------------------------------------------------------
git config user.name "Patrick Diandouke"
git config user.email "manjiadiandoukepatricksylvain@gmail.com"
echo [OK] Identite configuree : Patrick Diandouke

echo.
echo --- Attribution des anciens commits a votre compte ---
set GIT_SEQUENCE_EDITOR=true
set GIT_EDITOR=true
git rebase --root --committer-date-is-author-date --exec "git commit --amend --reset-author --no-edit" >nul 2>&1
if errorlevel 1 (
  git rebase --abort >nul 2>&1
  echo [INFO] Reattribution ignoree ^(sans consequence : le code sera publie normalement^).
) else (
  echo [OK] Les 4 commits sont maintenant a votre nom.
)
set GIT_SEQUENCE_EDITOR=
set GIT_EDITOR=

git add -A >nul 2>&1
git diff --cached --quiet
if errorlevel 1 (
  git commit -q -m "Preparer l hebergement en ligne : bornes de dependances, config et guides" >nul 2>&1
  echo [OK] Fichiers d'aide ajoutes au depot.
)

echo.
echo --- Etat du depot ---
git log --oneline -5
echo.

REM ---------------------------------------------------------------
REM  ETAPE 3 : identifiant GitHub
REM ---------------------------------------------------------------
echo ============================================================
echo   VOTRE IDENTIFIANT GITHUB
echo ============================================================
echo.
echo   Si vous ne vous en souvenez pas : ouvrez github.com,
echo   connectez-vous, cliquez sur votre photo en haut a droite.
echo   L'identifiant apparait juste sous votre nom.
echo.
set "GHUSER="
set /p GHUSER=Votre identifiant GitHub :
if "!GHUSER!"=="" (
  echo.
  echo [ERREUR] Aucun identifiant saisi. Relancez le script.
  echo.
  pause
  exit /b 1
)
echo.
echo   Identifiant retenu : !GHUSER!
echo   Le depot sera : https://github.com/!GHUSER!/pronostat
echo.

REM ---------------------------------------------------------------
REM  ETAPE 4 : creation du depot sur GitHub
REM ---------------------------------------------------------------
echo ============================================================
echo   CREATION DU DEPOT SUR GITHUB
echo ============================================================
echo.
echo   J'ouvre le formulaire deja pre-rempli dans votre navigateur.
echo.
echo   Il ne vous reste qu'a cliquer sur le bouton vert
echo   "Create repository" tout en bas de la page.
echo.
echo   IMPORTANT : ne cochez NI "Add a README file"
echo               NI "Add .gitignore" NI "Choose a license".
echo               Le depot doit rester vide.
echo.
echo   La case "Private" est deja cochee : votre code restera
echo   invisible pour les autres. Ne la decochez pas.
echo.
pause
start "" "https://github.com/new?name=pronostat&description=Agent+d+analyse+sportive+autonome&visibility=private"
echo.
echo   ^>^>^> Revenez ici une fois le depot cree sur GitHub.
echo.
pause

REM ---------------------------------------------------------------
REM  ETAPE 5 : envoi du code
REM ---------------------------------------------------------------
echo.
echo ============================================================
echo   ENVOI DU CODE VERS GITHUB
echo ============================================================
echo.
echo   Une fenetre de connexion GitHub peut s'ouvrir.
echo   Choisissez "Sign in with your browser" et autorisez.
echo   Aucun mot de passe a taper ici.
echo.

git remote remove origin >nul 2>&1
git remote add origin "https://github.com/!GHUSER!/pronostat.git"
git branch -M main
git push -u origin main

if errorlevel 1 (
  echo.
  echo ============================================================
  echo   L'ENVOI A ECHOUE
  echo ============================================================
  echo.
  echo   Causes les plus frequentes :
  echo.
  echo   - "Repository not found"
  echo       Le depot n'existe pas encore, ou l'identifiant est
  echo       mal orthographie. Verifiez github.com/!GHUSER!/pronostat
  echo.
  echo   - "Authentication failed"
  echo       La connexion a ete refusee ou annulee. Relancez
  echo       le script et acceptez l'autorisation dans le navigateur.
  echo.
  echo   - "Updates were rejected"
  echo       Le depot n'est pas vide. Recreez-le sans README.
  echo.
  echo   Copiez le message d'erreur ci-dessus et montrez-le moi.
  echo.
  pause
  exit /b 1
)

echo.
echo [OK] Code publie sur https://github.com/!GHUSER!/pronostat
echo.

REM ---------------------------------------------------------------
REM  ETAPE 6 : deploiement Streamlit Cloud
REM ---------------------------------------------------------------
echo ============================================================
echo   DEPLOIEMENT SUR STREAMLIT CLOUD
echo ============================================================
echo.
echo   J'ouvre Streamlit Cloud. Marche a suivre :
echo.
echo     1. "Continue with GitHub" puis autorisez l'acces
echo.
echo     2. DEPOT PRIVE - autorisation supplementaire obligatoire.
echo        Sans elle, votre depot n'apparaitra pas dans la liste.
echo        Cliquez sur votre nom en haut a droite ^> "Settings"
echo        ^> "Linked accounts" ^> sous "Source control",
echo        cliquez "Authorize" et acceptez sur GitHub.
echo        ^(Streamlit cree une cle de lecture seule ; GitHub
echo         vous enverra un e-mail de notification, c'est normal.^)
echo.
echo     3. Bouton "Create app" ^(ou "New app"^)
echo     4. Choisissez le deploiement depuis GitHub, puis
echo        renseignez exactement :
echo.
echo          Repository   : !GHUSER!/pronostat
echo          Branch       : main
echo          Main file    : app.py
echo.
echo     5. IMPORTANT - ouvrez "Advanced settings" et choisissez
echo        Python 3.12 AVANT de deployer. Cette version ne peut
echo        plus etre changee ensuite sans tout supprimer.
echo.
echo     6. Cliquez "Deploy"
echo.
echo   Le premier demarrage prend 2 a 5 minutes.
echo   Vous obtiendrez une adresse en .streamlit.app,
echo   utilisable sur ordinateur, Android, iPhone et tablette.
echo.
echo   L'application heritera de la confidentialite du depot :
echo   elle sera PRIVEE. Sur le telephone, connectez-vous avec
echo   la meme adresse e-mail que votre compte Streamlit.
echo   Pour l'ouvrir a tous : bouton "Share" ^> passer en Public.
echo.
pause
start "" "https://share.streamlit.io/"

echo.
echo ============================================================
echo   TERMINE
echo ============================================================
echo.
echo   Reste a faire une fois l'application en ligne :
echo   ouvrez APRES-DEPLOIEMENT.md dans ce dossier pour
echo   configurer les secrets ^(fichier a lire dans le Bloc-notes^).
echo.
pause
endlocal
