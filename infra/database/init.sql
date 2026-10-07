\getenv conversation_password CONVERSATION_DB_PASSWORD
\getenv scheduling_password SCHEDULING_DB_PASSWORD
\getenv evaluation_password EVALUATION_DB_PASSWORD
CREATE ROLE carepath_conversation LOGIN PASSWORD :'conversation_password';
CREATE ROLE carepath_scheduling LOGIN PASSWORD :'scheduling_password';
CREATE ROLE carepath_evaluation LOGIN PASSWORD :'evaluation_password';
CREATE DATABASE carepath_conversation OWNER carepath_conversation;
CREATE DATABASE carepath_scheduling OWNER carepath_scheduling;
CREATE DATABASE carepath_evaluation OWNER carepath_evaluation;
REVOKE ALL ON DATABASE carepath_conversation FROM PUBLIC;
REVOKE ALL ON DATABASE carepath_scheduling FROM PUBLIC;
REVOKE ALL ON DATABASE carepath_evaluation FROM PUBLIC;
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
GRANT CONNECT ON DATABASE carepath_conversation TO carepath_conversation;
GRANT CONNECT ON DATABASE carepath_scheduling TO carepath_scheduling;
GRANT CONNECT ON DATABASE carepath_evaluation TO carepath_evaluation;
