"""More onboarding fields, degree catalogue and qualification rows.

Revision ID: 0003_profile_qual
Revises: 0002_employee
"""

from alembic import op

revision = "0003_profile_qual"
down_revision = "0002_employee"
branch_labels = None
depends_on = None

# (code, label, level). Common Indian bachelor's and master's degree families, plus the
# few other credentials people list. "OTHER" lets HR record anything not listed, in words.
_DEGREES = [
    # Bachelor's
    ("BA", "Bachelor of Arts (B.A.)", "BACHELOR"),
    ("BSC", "Bachelor of Science (B.Sc.)", "BACHELOR"),
    ("BCOM", "Bachelor of Commerce (B.Com.)", "BACHELOR"),
    ("BBA", "Bachelor of Business Administration (BBA)", "BACHELOR"),
    ("BBM", "Bachelor of Business Management (BBM)", "BACHELOR"),
    ("BMS", "Bachelor of Management Studies (BMS)", "BACHELOR"),
    ("BAF", "Bachelor of Accounting and Finance (BAF)", "BACHELOR"),
    ("BFM", "Bachelor of Financial Markets (BFM)", "BACHELOR"),
    ("BBI", "Bachelor of Banking and Insurance (BBI)", "BACHELOR"),
    ("BCA", "Bachelor of Computer Applications (BCA)", "BACHELOR"),
    ("BTECH", "Bachelor of Technology (B.Tech.)", "BACHELOR"),
    ("BE", "Bachelor of Engineering (B.E.)", "BACHELOR"),
    ("BARCH", "Bachelor of Architecture (B.Arch.)", "BACHELOR"),
    ("BPLAN", "Bachelor of Planning (B.Plan.)", "BACHELOR"),
    ("BDES", "Bachelor of Design (B.Des.)", "BACHELOR"),
    ("BFA", "Bachelor of Fine Arts (BFA)", "BACHELOR"),
    ("BPA", "Bachelor of Performing Arts (BPA)", "BACHELOR"),
    ("BVOC", "Bachelor of Vocation (B.Voc.)", "BACHELOR"),
    ("BHM", "Bachelor of Hotel Management (BHM)", "BACHELOR"),
    ("BHMCT", "Bachelor of Hotel Management and Catering Technology (BHMCT)", "BACHELOR"),
    ("BJMC", "Bachelor of Journalism and Mass Communication (BJMC)", "BACHELOR"),
    ("BMM", "Bachelor of Mass Media (BMM)", "BACHELOR"),
    ("BSW", "Bachelor of Social Work (BSW)", "BACHELOR"),
    ("BED", "Bachelor of Education (B.Ed.)", "BACHELOR"),
    ("BELED", "Bachelor of Elementary Education (B.El.Ed.)", "BACHELOR"),
    ("BPED", "Bachelor of Physical Education (B.P.Ed.)", "BACHELOR"),
    ("BLISC", "Bachelor of Library and Information Science (B.Lib.I.Sc.)", "BACHELOR"),
    ("LLB", "Bachelor of Laws (LL.B.)", "BACHELOR"),
    ("BALLB", "B.A. LL.B. (Integrated)", "BACHELOR"),
    ("BBALLB", "BBA LL.B. (Integrated)", "BACHELOR"),
    ("BCOMLLB", "B.Com. LL.B. (Integrated)", "BACHELOR"),
    ("MBBS", "Bachelor of Medicine and Bachelor of Surgery (MBBS)", "BACHELOR"),
    ("BDS", "Bachelor of Dental Surgery (BDS)", "BACHELOR"),
    ("BAMS", "Bachelor of Ayurvedic Medicine and Surgery (BAMS)", "BACHELOR"),
    ("BHMS", "Bachelor of Homoeopathic Medicine and Surgery (BHMS)", "BACHELOR"),
    ("BUMS", "Bachelor of Unani Medicine and Surgery (BUMS)", "BACHELOR"),
    ("BSMS", "Bachelor of Siddha Medicine and Surgery (BSMS)", "BACHELOR"),
    ("BNYS", "Bachelor of Naturopathy and Yogic Sciences (BNYS)", "BACHELOR"),
    ("BPT", "Bachelor of Physiotherapy (BPT)", "BACHELOR"),
    ("BOT", "Bachelor of Occupational Therapy (BOT)", "BACHELOR"),
    ("BPHARM", "Bachelor of Pharmacy (B.Pharm.)", "BACHELOR"),
    ("BSCNURS", "B.Sc. Nursing", "BACHELOR"),
    ("BMLT", "Bachelor of Medical Laboratory Technology (BMLT)", "BACHELOR"),
    ("BOPTOM", "Bachelor of Optometry (B.Optom.)", "BACHELOR"),
    ("BASLP", "Bachelor of Audiology and Speech-Language Pathology (BASLP)", "BACHELOR"),
    ("BVSC", "Bachelor of Veterinary Science and Animal Husbandry (B.V.Sc. & A.H.)", "BACHELOR"),
    ("BFSC", "Bachelor of Fisheries Science (B.F.Sc.)", "BACHELOR"),
    ("BSCAGRI", "B.Sc. Agriculture", "BACHELOR"),
    ("BSCFOR", "B.Sc. Forestry", "BACHELOR"),
    # Master's
    ("MA", "Master of Arts (M.A.)", "MASTER"),
    ("MSC", "Master of Science (M.Sc.)", "MASTER"),
    ("MCOM", "Master of Commerce (M.Com.)", "MASTER"),
    ("MBA", "Master of Business Administration (MBA)", "MASTER"),
    ("MMS", "Master of Management Studies (MMS)", "MASTER"),
    ("MFC", "Master of Finance and Control (MFC)", "MASTER"),
    ("MCA", "Master of Computer Applications (MCA)", "MASTER"),
    ("MTECH", "Master of Technology (M.Tech.)", "MASTER"),
    ("ME", "Master of Engineering (M.E.)", "MASTER"),
    ("MARCH", "Master of Architecture (M.Arch.)", "MASTER"),
    ("MPLAN", "Master of Planning (M.Plan.)", "MASTER"),
    ("MDES", "Master of Design (M.Des.)", "MASTER"),
    ("MFA", "Master of Fine Arts (MFA)", "MASTER"),
    ("MPA", "Master of Performing Arts (MPA)", "MASTER"),
    ("MVOC", "Master of Vocation (M.Voc.)", "MASTER"),
    ("MJMC", "Master of Journalism and Mass Communication (MJMC)", "MASTER"),
    ("MCJ", "Master of Communication and Journalism (MCJ)", "MASTER"),
    ("MSW", "Master of Social Work (MSW)", "MASTER"),
    ("MED", "Master of Education (M.Ed.)", "MASTER"),
    ("MPED", "Master of Physical Education (M.P.Ed.)", "MASTER"),
    ("MLISC", "Master of Library and Information Science (M.Lib.I.Sc.)", "MASTER"),
    ("LLM", "Master of Laws (LL.M.)", "MASTER"),
    ("MD", "Doctor of Medicine (MD)", "MASTER"),
    ("MS", "Master of Surgery (MS)", "MASTER"),
    ("MDS", "Master of Dental Surgery (MDS)", "MASTER"),
    ("MPHARM", "Master of Pharmacy (M.Pharm.)", "MASTER"),
    ("MPT", "Master of Physiotherapy (MPT)", "MASTER"),
    ("MOT", "Master of Occupational Therapy (MOT)", "MASTER"),
    ("MSCNURS", "M.Sc. Nursing", "MASTER"),
    ("MPH", "Master of Public Health (MPH)", "MASTER"),
    ("MHA", "Master of Hospital Administration (MHA)", "MASTER"),
    ("MVSC", "Master of Veterinary Science (M.V.Sc.)", "MASTER"),
    ("MFSC", "Master of Fisheries Science (M.F.Sc.)", "MASTER"),
    ("MSCAGRI", "M.Sc. Agriculture", "MASTER"),
    ("MPHIL", "Master of Philosophy (M.Phil.)", "MASTER"),
    # Other credentials people list
    ("SSC", "Secondary School (10th)", "OTHER"),
    ("HSC", "Higher Secondary (12th)", "OTHER"),
    ("ITI", "ITI", "OTHER"),
    ("DIPLOMA", "Diploma", "OTHER"),
    ("PGDM", "Post Graduate Diploma in Management (PGDM)", "OTHER"),
    ("PGD", "Other Post Graduate Diploma", "OTHER"),
    ("CA", "Chartered Accountant (CA)", "OTHER"),
    ("CS", "Company Secretary (CS)", "OTHER"),
    ("CMA", "Cost and Management Accountant (CMA)", "OTHER"),
    ("DMMCH", "DM / M.Ch.", "OTHER"),
    ("PHARMD", "Doctor of Pharmacy (Pharm.D.)", "OTHER"),
    ("PHD", "Doctor of Philosophy (Ph.D.)", "OTHER"),
    ("OTHER", "Other (type the name)", "OTHER"),
]


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE hr.degree (
            code       text PRIMARY KEY,
            label      text NOT NULL,
            level      text NOT NULL CHECK (level IN ('BACHELOR', 'MASTER', 'OTHER')),
            sort_order integer NOT NULL,
            active     boolean NOT NULL DEFAULT true
        )
        """
    )
    for order, (code, label, level) in enumerate(_DEGREES, start=1):
        op.execute(
            "INSERT INTO hr.degree (code, label, level, sort_order) VALUES ("
            f"'{code}', '{label.replace(chr(39), chr(39) * 2)}', '{level}', {order})"
        )

    # A person can hold several (a bachelor's and a master's). Percentage is marks as a percent.
    op.execute(
        """
        CREATE TABLE hr.employee_qualification (
            qualification_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            employee_id      uuid NOT NULL REFERENCES hr.employee (employee_id),
            degree_code      text NOT NULL REFERENCES hr.degree (code),
            degree_other     text,
            percentage       numeric(5, 2) NOT NULL CHECK (percentage >= 0 AND percentage <= 100),
            year_of_passing  integer NOT NULL CHECK (year_of_passing BETWEEN 1950 AND 2100),
            created_at       timestamptz NOT NULL DEFAULT now(),
            created_by       text NOT NULL,
            updated_at       timestamptz NOT NULL DEFAULT now(),
            updated_by       text NOT NULL,
            CONSTRAINT other_degree_named CHECK (degree_code <> 'OTHER' OR degree_other IS NOT NULL)
        )
        """
    )
    op.execute(
        "CREATE INDEX employee_qualification_employee_idx ON hr.employee_qualification (employee_id)"
    )

    op.execute(
        """
        ALTER TABLE hr.employee
            ADD COLUMN state text,
            ADD COLUMN pincode text CHECK (pincode ~ '^[1-9][0-9]{5}$'),
            ADD COLUMN total_experience_years numeric(4, 1)
                CHECK (total_experience_years >= 0 AND total_experience_years <= 60),
            ADD COLUMN emergency_contact_address text,
            ADD COLUMN photo_updated_at timestamptz
        """
    )


def downgrade() -> None:
    raise RuntimeError("HR migrations are forward-only")
