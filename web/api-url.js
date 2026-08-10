/* -------------------------------------------------------------------------
 * ADRESSE DE L'API — le seul endroit à modifier.
 *
 * Laissée vide, l'interface appelle la même origine qu'elle : c'est le cas
 * quand elle est servie par l'API elle-même (Hugging Face Space).
 *
 * Renseignez-la quand l'interface est hébergée ailleurs — sur Firebase par
 * exemple — avec l'adresse complète du service, sans barre oblique finale :
 *
 *     window.PRONOSTAT_API = "https://patrickdke-pronostat-api.hf.space";
 *
 * L'adresse doit être en HTTPS. Une page servie en HTTPS ne peut pas appeler
 * une adresse en HTTP : le navigateur bloque, et rien ne s'affiche.
 * ---------------------------------------------------------------------- */

window.PRONOSTAT_API = "";
