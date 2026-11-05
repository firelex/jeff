CREATE TABLE "data_set_upload" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"issued_at" timestamp with time zone NOT NULL
);
--> statement-breakpoint
ALTER TABLE "data_set_upload" ADD CONSTRAINT "data_set_upload_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "data_set_upload_user_issued" ON "data_set_upload" USING btree ("user_id","issued_at");--> statement-breakpoint
CREATE INDEX "data_set_upload_issued" ON "data_set_upload" USING btree ("issued_at");