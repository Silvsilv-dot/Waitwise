-- Optional: log edits made directly in Neon (SQL editor / Tables view) into the same activity log.
-- The app connects with application_name = 'waitwise-app' and already writes its own audit rows,
-- so this trigger skips those and only records changes made from anywhere else.
-- Run once in the Neon SQL Editor.

CREATE OR REPLACE FUNCTION waitroom.log_appointment_change() RETURNS trigger AS $$
BEGIN
  IF current_setting('application_name', true) = 'waitwise-app' THEN
    RETURN COALESCE(NEW, OLD);
  END IF;

  IF TG_OP = 'INSERT' THEN
    INSERT INTO waitroom.audit_log(user_role, user_id, action_type, table_affected, record_id, old_values_json, new_values_json)
    VALUES ('database', 'neon-console', 'DB_INSERT', 'appointments', NEW.appointment_id::text, NULL,
            jsonb_build_object('patient_id', NEW.patient_id, 'provider_id', NEW.provider_id, 'start', NEW.start_time_est, 'status', NEW.status));
  ELSIF TG_OP = 'UPDATE' THEN
    INSERT INTO waitroom.audit_log(user_role, user_id, action_type, table_affected, record_id, old_values_json, new_values_json)
    VALUES ('database', 'neon-console', 'DB_UPDATE', 'appointments', NEW.appointment_id::text,
            jsonb_build_object('provider_id', OLD.provider_id, 'start', OLD.start_time_est, 'status', OLD.status),
            jsonb_build_object('provider_id', NEW.provider_id, 'start', NEW.start_time_est, 'status', NEW.status));
  ELSE
    INSERT INTO waitroom.audit_log(user_role, user_id, action_type, table_affected, record_id, old_values_json, new_values_json)
    VALUES ('database', 'neon-console', 'DB_DELETE', 'appointments', OLD.appointment_id::text,
            jsonb_build_object('provider_id', OLD.provider_id, 'start', OLD.start_time_est, 'status', OLD.status), NULL);
  END IF;
  RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_appointments_audit ON waitroom.appointments;
CREATE TRIGGER trg_appointments_audit
AFTER INSERT OR UPDATE OR DELETE ON waitroom.appointments
FOR EACH ROW EXECUTE FUNCTION waitroom.log_appointment_change();
