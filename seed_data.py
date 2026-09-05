"""
Seed data for the Module E lookup tables — the PROPOSED values from
PIMS_Phase1_Design_Specification.docx §3.1.

Countries use the full ISO 3166-1 alpha-2 list (source: iban.com/country-codes,
cross-checked against the ISO 3166 standard), minus eight uninhabited/
no-permanent-population territories (Antarctica, Bouvet Island, British
Indian Ocean Territory, French Southern Territories, Heard Island and
McDonald Islands, South Georgia and the South Sandwich Islands, Svalbard
and Jan Mayen, United States Minor Outlying Islands) that have no realistic
use in a personal contacts app. That's 241 of the 249 ISO entries. Names
are the common/practical form (e.g. "Bolivia" not "Bolivia (Plurinational
State of)") rather than the full ISO official name.

Calling codes (source: Wikipedia "List of country calling codes", itself
sourced from the ITU-T E.164 numbering plan) are seeded for every country
above. NANP members (US, Canada, and Caribbean/Pacific territories that
share the +1 country code) are seeded as "+1-NNN" with their distinguishing
area code, since a bare "+1" doesn't distinguish them. A handful of
territories that don't have their own ITU country code (Åland, Jersey,
Guernsey, Isle of Man, Western Sahara, Saint Barthélemy, Saint Martin,
Bonaire/Sint Eustatius/Saba, Cocos Islands, Christmas Island, Pitcairn) are
seeded with the calling code of the country whose numbering plan they use.

Not yet in scope: full ISO 3166-2 state/province subdivisions for every
country. `states` is seeded with all 50 US states + DC, plus Canada's 10
provinces and 3 territories — the two countries most likely to need
state-level detail for a US-based user. Extending this to another
country's subdivisions is a matter of adding another block below.

Safe to re-run: uses INSERT OR IGNORE keyed on each table's `code`
(states are additionally scoped by country_id).
"""

CONTACT_CATEGORIES = [
    "Personal", "Classmate in Institution", "Colleague in Organization",
    "Friend", "Immediate Family", "Close Relative", "Distant Relative",
    "Doctor", "Lawyer", "Teacher", "Accountant", "Knowledge Expert",
]

CONTACT_TITLES = [
    "Mr.", "Mrs.", "Miss", "Dr.", "Rai.", "Sir.", "Lord.", "Hon.", "Eng.",
    "Dame", "Lady", "Madam", "Justice.",
]

CONTACT_SUFFIXES = [
    "PhD.", "Barrister", "Retd.", "Jr.", "Sr.", "SJ.", "HJ.", "Esq.",
    "CPA.", "M.D.", "D.D.S.", "D.V.M.",
]

# Three spellings corrected from the original request: "Masonary" -> "Masonry",
# "Babby Sitter" -> "Baby Sitter", "Gardner" -> "Gardener".
PROFESSIONS = [
    "Plumber", "Electrician", "Carpenter", "Welder", "Masonry", "Painter",
    "Decorator", "Cook", "Baby Sitter", "Security Guard", "General Help",
    "Registered Nurse", "Dentist", "Physician", "Seamstress", "Handyman",
    "Cleaner", "Driver", "Auto Mechanic", "Teacher", "Tutor", "Gardener",
    "Architect", "Engineer", "Pilot", "Waiter", "Personal Care",
]

# One spelling corrected from the original request: "ME Exoert" -> "ME Expert".
CONTACT_CONTEXTS = [
    "Software Dev.", "Sikh Tourism", "AI Expert", "ME Expert", "Resort Guest",
    "Potential Investor",
]

ORGANIZATION_TYPES = [
    "Software", "Bank", "Insurance Company", "US Govt.", "University/School",
    "Hospital", "Airline", "Travel Agency", "Restaurant", "Hotel", "Resort",
    "Car Rental", "Taxi", "Moving Company", "Plumber", "Carpenter",
    "Construction", "Law Offices", "Tax Services", "Brokerage",
    "Import-Export Company", "Cook", "Childcare/Babysitting",
    "Electronics Store", "Computer Repair", "Security Services", "Hyper Store",
]

# Deliberately its own list, not a reuse of ORGANIZATION_TYPES above —
# Suppliers keeps a fully separate type/sub-type table from Organizations
# (see schema.sql MODULE C). "Airlines" is an 11th type added here that
# wasn't in the request's explicit Supplier Type list, but the request did
# give it a sub-type list (Commercial/Charter/Other) — added rather than
# silently dropping that data; flagged here and in the delivery notes.
SUPPLIER_TYPES = [
    "Hotel", "Resort", "Transport Provider", "Catering Service", "Restaurant",
    "Security Services", "Travel Agent", "Tour Operator", "Marketing Service",
    "Printing Service", "Airlines",
    # Added for the "Sikh Pilgrimage Sector" AI Agent / Data Enrichment
    # pilot (Sept 2026): request item #6, "Local Tour Guides" -- a person/
    # small operation offering guided walking tours at a site, distinct
    # from "Travel Agent" (books/sells trips) and "Tour Operator" (designs/
    # runs the trip end-to-end).
    "Tour Guide",
]

# One level of nesting under SUPPLIER_TYPES (supplier_subtypes.supplier_type_id).
# Keyed by the exact label above. Types with no sub-type list here
# (Catering Service, Security Services, Marketing Service, Printing
# Service) simply have none seeded — sub-type is optional, same as
# knowledge_subdomains/content_subtypes elsewhere in this schema.
#
# Two notes on reconciling the request's wording: the sub-type list was
# headed "Travel Agency" but the Supplier Type list spells it "Travel
# Agent" — filed under "Travel Agent" here, since that's the actual type
# row it nests under. "outbound" was capitalized to "Outbound" to match
# the Title Case of everything else in this list.
SUPPLIER_SUBTYPES = {
    "Hotel": ["Five Star", "4-Star", "3-Star", "Apartment Suite", "Motel"],
    "Resort": ["Five Star", "4-Star", "3-Star"],
    "Transport Provider": ["Bus Services", "Car Rentals", "Full Service (Bus, Mini-Bus, Cars)"],
    "Restaurant": [
        "French", "Italian", "Pizza", "Indian", "Greek", "Turkish", "Japanese",
        "Chinese", "Thai", "Mexican", "Pakistani", "Punjabi", "Indonesian",
        "Fast Food", "Coffee Shop", "Continental",
    ],
    "Travel Agent": ["Domestic", "International", "Specialized", "Other"],
    "Tour Operator": [
        "Inbound", "Outbound", "Destination Management Company", "Ground Handlers",
        "Educational Tour Operators", "Sports Tour Operators", "Specialized Tour Operators",
    ],
    "Airlines": ["Commercial", "Charter", "Other"],
    # Added alongside the "Tour Guide" Supplier Type above.
    "Tour Guide": [
        "Government-Licensed Guide", "Freelance Guide", "Multilingual Guide", "Other",
    ],
}

# A supplier's several address locations (Main Office, Billing, ...) —
# schema.sql's supplier_address_types lookup, used by supplier_addresses.
SUPPLIER_ADDRESS_TYPES = ["Main Office", "Billing Address"]

# An organization's several address locations — schema.sql's
# organization_address_types lookup, used by organization_addresses. Kept
# separate from SUPPLIER_ADDRESS_TYPES above per the standing Organizations/
# Suppliers separation rule. No explicit list was given for Organizations
# (unlike Suppliers' "Main Office"/"Billing Address"), so these two are a
# reasonable default — editable any time via Table Maintenance.
ORGANIZATION_ADDRESS_TYPES = ["Mailing Address", "Physical Address"]

# An organization's several phone numbers — schema.sql's
# organization_phone_types lookup, used by organization_phones. Kept
# separate from Suppliers' phone_types, same reasoning as
# ORGANIZATION_ADDRESS_TYPES above. No explicit list was given, so this is
# a reasonable default (mirroring the shape of contacts' hardcoded phone
# types, minus "Home"/"WhatsApp" which don't fit an organization as well) —
# editable any time via Table Maintenance.
ORGANIZATION_PHONE_TYPES = ["Office", "Mobile", "Fax"]

# A supplier contact's several phone numbers (schema.sql's new phone_types
# lookup, used by supplier_contact_phones) — "Fax" is one of these rather
# than a separate column, per the request's "Maintain a separate Phone
# Table for phone numbers... phone type to identify cell, office, fax etc."
SUPPLIER_PHONE_TYPES = ["Cell", "Office", "Fax"]

# ---------------------------------------------------------------------------
# Human Resource Module (Human_Resource_Module1a.docx)
# ---------------------------------------------------------------------------

# Host Organization's own office/location addresses — one combined list per
# spec decision (not separate "Corporate"/"Location" tables). "Head Office"
# is seeded with is_head_office=1 (see _seed_host_address_types below) so
# the hard "only one address may be flagged Head Office" rule has something
# to check against that survives a relabel.
HOST_ADDRESS_TYPES = ["Head Office", "Branch Office", "Regional Office", "Warehouse"]

# Host Organization's own phone numbers — mirrors ORGANIZATION_PHONE_TYPES;
# "Fax" is required here specifically since an Employee's business card
# pulls its Fax number from the Host Organization/office (see
# blueprints/employees.py).
HOST_PHONE_TYPES = ["Office", "Mobile", "Fax"]

GENDERS = ["Male", "Female", "Other"]

EMPLOYEE_TYPES = ["Intern", "Full-time", "Part-time", "Trainee"]

DEPARTMENTS = [
    "Executive", "Operations", "HR and Recruitment", "Payroll",
    "Finance- Accounts and Invoicing", "Legal", "Secretarial",
    "Inventory Management", "Facilities Maintenance",
    "Sales/Marketing/Reservations", "Public Relations", "Security",
    "Computers and Information Technology",
]

JOB_TITLES = [
    "Chairman", "CEO", "President", "Vice President", "CFO", "CIO",
    "Department Manager", "Assistant Manager", "Supervisor", "Coordinator",
    "Accountant", "Executive Assistant", "Systems Engineer",
    "Database Supervisor", "Network Supervisor", "Systems Architect",
    "Software Programmer", "Graphics Designer", "Content Supervisor",
]

# An Employee's own phone numbers (employee_phone_types) — mirrors
# ORGANIZATION_PHONE_TYPES; kept as its own, separate lookup per the
# standing "each module gets its own type lookups" rule.
EMPLOYEE_PHONE_TYPES = ["Office", "Mobile", "Home", "Fax"]

RESOURCE_TYPES = [
    "Tours", "Transport", "Audit", "Sales/Marketing", "IT Helper",
    "IT Maintenance", "Facility Maintenance", "Cleaning", "Security",
    "Medical", "Other",
]

# Common to both Employees and External Resources (spec §(c)) — a person
# can hold more than one of these at once.
HR_ROLES = [
    "Tour Booking", "Tour Manager", "Reservations Agent",
    "Operations Coordinator", "Tour Guide / Tour Leader",
    "Travel Agent / Tour Reseller (B2B)", "Systems Administrator",
    "Accounts Payable", "Accounts Receivable and Collections", "Invoicing",
    "Purchasing", "IT Security", "General IT", "Network Specialist",
    "Computer Repair", "Building maintenance", "Electrician", "Plumber",
    "Cleaner", "Driver", "Doctor/Physician", "Nurse", "Baby Sitter",
    "Receptionist", "Executive Secretary", "Data Entry", "Other",
]

POI_TYPES = [
    "Sikh Gurdwara", "Mosque", "Mandir", "Museum", "Zoo", "Botonical Garden",
    "Historic Castle", "Historical Architecture", "Historical Fort",
    "Fish Farm", "Agri-Farm", "Amusement Park", "Ski Site", "Historic Library",
    "Other",
    # Added for the "Sikh Pilgrimage Sector" AI Agent / Data Enrichment pilot
    # (Sept 2026): request items #4/#5/#9/#10 -- Hospitals, Police Stations,
    # Airports and Rail Systems are themselves POIs a Tour Planner needs on
    # the map (nearest hospital/police station for a Day's stop; nearest
    # airport/rail link for routing) alongside the sightseeing attractions
    # already in this list.
    "Airport", "Railway Station", "Hospital", "Police Station",
]
# "Botonical Garden" is kept exactly as given in the request — not "Botanical".

GURDWARA_POIS = [
    # (name, city, province, notes) — reconciled from LIST OF ALL SIKH
    # GURDWARAS IN PAKISTAN1a.docx. city is None for the one row ("Dharam
    # Shala, Pehli Patshahi") that listed no city at all — seeded with both
    # city_id and city_text left blank, province (Sindh) still resolved.
    # See PAKISTAN_CITIES above for the handful of province/city corrections
    # applied against real-world geography.
    ("Gurdwara Pehli Patshahi, Kallat", "Kallat", "Baluchistan", ""),
    ("Gurdwara Tilgenji Sahib, Quetta", "Quetta", "Baluchistan", ""),
    ("Gurdwara Bhai Than Singh, Attock", "Attock", "Punjab", ""),
    ("Gurdwara Pehli Patshahi, Balakot (Hazara)", "Balakot", "Khyber Pakhtunkhwa", ""),
    ("Gurdwara Bhai Bannu Mangat", "Bannu", "Khyber Pakhtunkhwa", ""),
    ("Gurdwara Danna Khel, Bannu", "Bannu", "Khyber Pakhtunkhwa", ""),
    ("Gurdwara Jogiwara, Bannu", "Bannu", "Khyber Pakhtunkhwa", ""),
    ("Gurdwara Chhevin Patshahi, Dhamial", "Dhamial", "Khyber Pakhtunkhwa", ""),
    ("Gurdwara Chhevin Patshahi, Chitti Gatti, Mansehra, Distt. Hazara", "Mansehra", "Khyber Pakhtunkhwa", ""),
    ("Gurdwara Chhevin Patshahi, Mansehra", "Mansehra", "Khyber Pakhtunkhwa", ""),
    ("Gurdwara Patshahi, Naralil", "Naralil", "Khyber Pakhtunkhwa", ""),
    ("Gurdwara Bhai Joga Singh, Peshawar", "Peshawar", "Khyber Pakhtunkhwa", ""),
    ("Gurdwara Gurhattti, Peshawar", "Peshawar", "Khyber Pakhtunkhwa", ""),
    ("Gurdwara Sri Diyal Sar Topi, Rakh Topi,  KPK", "Rakh Topi", "Khyber Pakhtunkhwa", ""),
    ("Gurdwara Pehli Patshahi, Balakot", "Balakot", "Khyber Pakhtunkhwa", ""),
    ("Gurdwara Chota Nanakiana, Sakardu", "Sakardu", "Gilgit Baltisitan", ""),
    ("Gurdwara Dharam Shala, Pehli Patshahi", "Lahore", "Punjab", ""),
    ("Gurdwara Panjvin Patshahi, Sheikhrumi", "Lahore", "Punjab", ""),
    ("Gurdwara Shala, Bhai Hamam Singh Ji", "Bucheki", "Punjab", ""),
    ("Gurdwara Shikar Gurah Sahib Kachha", "Kachha", "Punjab", ""),
    ("Gurdwara Puncham Patshahi, Beharwal, Distt. Kasur", "Beharwal", "Punjab", ""),
    ("Gurdwara Pehli Patshahi, Burewala", "Burewala", "Punjab", ""),
    ("Khuh Baba Farid 17 Eb, Burewala", "Burewala", "Punjab", ""),
    ("Gurdwara Panjvin Patshahi, Chak Ram Das, Gujranwala Distt.", "Chak Ram Das", "Punjab", ""),
    ("Gurdwara Pehli Patshahi, Kattas, Chakwal", "Chakwal", "Punjab", ""),
    ("Gurdwara Ameer Shah Ji, D.I. Khan", "Dera Ismail Khan", "Khyber Pakhtunkhwa", ""),
    ("Gurdwara Dharamshala Guru Nanak Dev Ji", "Dera Ismail Khan", "Khyber Pakhtunkhwa", ""),
    ("Gurdwara Kali Devi, D.I.Khan", "Dera Ismail Khan", "Khyber Pakhtunkhwa", ""),
    ("Gurdwara Darbar Sri Chand Bhumman Shah, 18Km from Dipalpur", "Dipalpur", "Punjab", ""),
    ("Gurdwara Chota Nankiana, Dipalpur, Okara", "Dipalpur", "Punjab", ""),
    ("Gurdwara Chhevin Patshahi, Kotli Bhaga Village, Eimenabad", "Eimenabad", "Punjab", ""),
    ("Gurdwara Bhai Lalu Di Khuhi, Eimenabad", "Eimenabad", "Punjab", ""),
    ("Gurdwara Chaki Sahib, Eimenabad", "Eimenabad", "Punjab", ""),
    ("Gurdwara Rohri Sahib, Eimenabad", "Eimenabad", "Punjab", ""),
    ("Gurdwara Shaheed Bhai Daleep Singh Ji, Chak 132 RB (Chak Jhumra)", "Faisalabad", "Punjab", ""),
    ("Gurdwara Sacha Sauda, Choorhkana/Farooqabad", "Farooqabad", "Punjab", ""),
    ("Gurdwara Sach Khand, Choorhkana/Farooqabad", "Farooqabad", "Punjab", ""),
    ("Gurdwara Pehli Patshahi Fateh Bhinder (Sialkot Distt.)", "Fateh Bhinder", "Punjab", ""),
    ("Gurdwara Damdma Saheb, Gujranawala", "Gujranwala", "Punjab", ""),
    ("Gurdwara Chhevin Patshahi, Galotian Kalan, Near Gujranwala", "Gujranwala", "Punjab", ""),
    ("Gurdwara Bhai Lalu, Village Tatliali, District Gujranwala", "Gujranwala", "Punjab", ""),
    ("Gurdwara Khara Sahib, Bhaike Mattu (2Km from Noshehra' Virkan, Tehsil Gujranwala)", "Gujranwala", "Punjab", ""),
    ("Gurdwara Chhevin Patshahi, Bazurgwal Village, Union Council of Gujrat District", "Gujrat", "Punjab", ""),
    ("Gurdwara Chhevin Patshahi, Gujrat", "Gujrat", "Punjab", ""),
    ("Gurdwara NanakSar, Dinga, Gujrat", "Gujrat", "Punjab", ""),
    ("Mizar Hazrat Shah Daula, Gujrat", "Gujrat", "Punjab", ""),
    ("Gurdwara Chhevin Patshahi, Hafizabad", "Hafizabad", "Punjab", ""),
    ("Gurdwara Guru Ramdas Ji Dhuni, Hafizabad", "Hafizabad", "Punjab", ""),
    ("Gurdwara Ichhaprik Vinni, Hafizabad", "Hafizabad", "Punjab", ""),
    ("Gurdwara Pind Bachhe, Hafizabad", "Hafizabad", "Punjab", ""),
    ("Gurdwara NanakSar, Harrapa", "Harrapa", "Punjab", ""),
    ("Gurdwara Punja Sahib, Hassan Abdal", "Hassan Abdal", "Punjab", ""),
    ("Gurdwara Kir Ji Sahib, Jaesukhwala", "Jaesukhwala", "Punjab", ""),
    ("Gurdwara Bhai Karam Singh, Jehlum", "Jhelum", "Punjab", ""),
    ("Gurdwara Pehli Patshahi Bal Gundai, Tila Joggian, Jehlum", "Jhelum", "Punjab", ""),
    ("Gurdwara Bhai Khan Chand at Magghiana, Distt. Jhang", "Jhang", "Punjab", ""),
    ("Gurdwara Garh Shah Fatah, Jhang", "Jhang", "Punjab", ""),
    ("Gurdwara NanakSar, Jhang", "Jhang", "Punjab", ""),
    ("Gurdwara Dharam Shala Bhai Hema Ji Magghiana, Jhang", "Jhang", "Punjab", ""),
    ("Gurdwara Darbar Sahib, Kartarpur, Narowal", "Kartarpur", "Punjab", ""),
    ("Gurdwara Baba Ram Thaman Ji, Kasur", "Kasur", "Punjab", ""),
    ("Gurdwara Bhai Behiul Qadivind, Kasur", "Kasur", "Punjab", ""),
    ("Gurdwara Haulm Sahib Bhamavan, Kasur", "Kasur", "Punjab", ""),
    ("Gurdwara Jhari Sahib Targe, Kasur", "Kasur", "Punjab", ""),
    ("Gurdwara Maal Ji Sahib, KanganPur, Kasur", "Kasur", "Punjab", ""),
    ("Gurdwara Manji Sahib Manak Deke, Kasur", "Kasur", "Punjab", ""),
    ("Gurdwara Pehli Patshahi Alpa, Kasur", "Kasur", "Punjab", ""),
    ("Gurdwara Pehli Patshahi Bheelgrani, Kasur", "Kasur", "Punjab", ""),
    ("Gurdwara Tham Sahib Jumber, Kasur", "Kasur", "Punjab", ""),
    ("Gurdwara Janam Asthan Mata Saheb Kaur Ji", "Kaur", "Punjab", ""),
    ("Gurdwara Chhevin Patshahi, Chungi Amarsadhu,", "Lahore", "Punjab", ""),
    ("Gurdwara Chhevin Patshahi, Guru Mangat, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Chhevin Patshahi, Jhallian, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Chhevin Patshahi, Minhala Kalan, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Chhevin Patshahi, Padhana Village, Distt, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Chhevin Patshahi, Rampura, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Baoli Sahib, Rang Mahal, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Beri Sahib Kharak, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Budhu Da Aava, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Chaubacha Sahib, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Chaumala Sahib, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Chhevin Patshahi, Hadyara Village, Distt Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Chhevin Patshahi, Mozang, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Chota Nankiana, Manga, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Dera Sahib, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Devan Khana, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Janam Asthan Bebe Nanaki, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Janam Asthan, Sat Guru Ram Dass Ji, Chuna Mandi Bazar, Dehli Darwaza", "Lahore", "Punjab", ""),
    ("Gurdwara Laal Khooh, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Lahora Sahib, Jahman, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Nanak Garh, Badami Bagh, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Pehli Patshahi Manak, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Samadhi Baba Shri Chand, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Shaheed Gunj Bhai Mani Singh Ji, Masti Gate", "Lahore", "Punjab", ""),
    ("Gurdwara Shaheed Gunj Bhai Taroo Singh Ji, Naulakha Bazar", "Lahore", "Punjab", ""),
    ("Gurdwara Shaheed Gunj Singh Singhania, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Tibba Baba Farid, Lahore", "Lahore", "Punjab", ""),
    ("Mizar Sain Hazrat Mian Meer Ji", "Lahore", "Punjab", ""),
    ("Samadh Baba Shri Chand, Lahore", "Lahore", "Punjab", ""),
    ("Gurdwara Pehli Patshahi, Chohatta Mufti Baqar", "Lahore", "Punjab", ""),
    ("Gurdwara Bal Lila, Nankana Sahib", "Nankana Sahib", "Punjab", ""),
    ("Gurdwara Sacchi Manji Pehli Patshahi, Haftmadar", "Nankana Sahib", "Punjab", ""),
    ("Gurdwara Janam Asthan, Nankana Sahib", "Nankana Sahib", "Punjab", ""),
    ("Gurdwara Kabar Rai Bular, Nankana Sahib", "Nankana Sahib", "Punjab", ""),
    ("Gurdwara Kirara Sahib, Nankana Sahib", "Nankana Sahib", "Punjab", ""),
    ("Gurdwara Maal Ji Sahib, Nankana Sahib", "Nankana Sahib", "Punjab", ""),
    ("Gurdwara Panjvin & Chhevin Patshahi, Nankana Sahib", "Nankana Sahib", "Punjab", ""),
    ("Gurdwara Panjvin Patshahi, Jatri Village, (Bhai Pheru-Khunda / Balluki Head Work)", "Nankana Sahib", "Punjab", ""),
    ("Gurdwara Patti Sahib, Nankana Sahib", "Nankana Sahib", "Punjab", ""),
    ("Gurdwara Tamboo Sahib, Nankana Sahib", "Nankana Sahib", "Punjab", ""),
    ("Gurdwara Pehli Patshahi, Mallah village, Shah Gharib (9 miles from Narowal)", "Narowal", "Punjab", ""),
    ("Gurdwara Chota Nankiana, Hujra Shah Muqeem, Okara", "Okara", "Punjab", ""),
    ("Gurdwara Bhuman Shah, Distt. Okara", "Okara", "Punjab", ""),
    ("Gurdwara Chota Nankiana, Okara", "Okara", "Punjab", ""),
    ("Gurdwara NanakSar, Tibba Abhor, Distt Pakpattan", "Pakpattan", "Punjab", ""),
    ("Gurdwara Pehli Patshahi, Pakpattan", "Pakpattan", "Punjab", ""),
    ("Gurdwara Tibba Nanaksar, Pakpattan", "Pakpattan", "Punjab", ""),
    ("Gurdwara Panjvin Patshahi, Hanjra, (near Pattoki / Lahore)", "Pattoki", "Punjab", ""),
    ("Gurdwara Malrhdoompur, Pohran", "Pohran", "Punjab", ""),
    ("Gurdwara Pehli Patshahi Deoke, Pusrur", "Pusrur", "Punjab", ""),
    ("Gurdwara Chhevin Patshahi, Rasool Nagar", "Rasool Nagar", "Punjab", ""),
    ("Gurdwara Bhai Mani Singh, Rawalpindi", "Rawalpindi", "Punjab", ""),
    ("Gurdwara Nirankari, Rawalpindi", "Rawalpindi", "Punjab", ""),
    ("Gurdwara Singh Sabah, Rawalpindi", "Rawalpindi", "Punjab", ""),
    ("Gurdwara Choa Sahib, Rothas", "Rohtas", "Punjab", ""),
    ("Dargah Baba Farid Ganj Shakar", "Sahiwal", "Punjab", ""),
    ("Gurdwara Nanaksar, Sahiwal", "Sahiwal", "Punjab", ""),
    ("Gurdwara Pehli Patshahi, Seoke", "Seoke", "Punjab", ""),
    ("Gurdwara Ajnianwala, 20 miles from Sheikhupura on Hafizabad Road", "Sheikhupura", "Punjab", ""),
    ("Chillah Gah Hazrat Hamza Ghaus, (near Gurdwara Baba Bair Sahib, Mohalla Baba Bair)", "Sialkot", "Punjab", ""),
    ("Gurdwara Baoli Sahib, Sialkot", "Sialkot", "Punjab", ""),
    ("Gurdwara Ber Sahib, Sialkot", "Sialkot", "Punjab", ""),
    ("Gurdwara Chhevin Patshahi, Dhilwan", "Sialkot", "Punjab", ""),
    ("Gurdwara Gurusar Rehsma, Sialkot", "Sialkot", "Punjab", ""),
    ("Gurdwara Nanaksar Tilakpur, Sialkot", "Sialkot", "Punjab", ""),
    ("Gurdwara Talhi Sahib Rehsma, Sialkot", "Sialkot", "Punjab", ""),
    ("Gurdwara Thara Sahib, Uchsharif", "Uch Sharif", "Punjab", ""),
    ("Gurdwara Guru Da Kotha, Wazirabad", "Wazirabad", "Punjab", ""),
    ("Gurdwara NanakVara, Kandhkot", "Kandhkot", "Sindh", ""),
    ("Gurdwara Pehli Patshahi Clifton, Karachi", "Karachi", "Sindh", ""),
    ("Gurdwara Pehli Patshahi, Karachi", "Karachi", "Sindh", ""),
    ("Gurdwara Pehli Patshahi Bulani, Larkana", "Larkana", "Sindh", ""),
    ("Gurdwara Pehli Patshahi, Mirpur Khas", "Mirpur Khas", "Sindh", ""),
    ("Gurdwara Thara Sahib, Sakhi Sarwar", "Sakhi Sarwar", "Punjab", ""),
    ("Gurdwara Pehli Patshahi, Shikarpur Sindh", "Shikarpur", "Sindh", ""),
    ("Gurdwara Pehli Patshahi, Jindpur Sukkhur", "Sukkur", "Sindh", ""),
    ("Gurdwara Sadhu Bela, Sukkur", "Sukkur", "Sindh", ""),
    ("Dharam Shala, Bhai Harnam Singh Ji", "Bucheki", "Punjab", "The historical Dharamsala of Bhai Harnam Singh Ji is located in the old town of Buccheki (Bucheke), in the Sheikhupura district of Punjab.  \nRegion: Buccheki is situated along the Lahore-Jaranwala road in Pakistan.\nLocal Area: The site is in the old town area known locally as Mohalla Dharamsala.\nHistorical Significance: This site commemorates a visit and stay by the fifth Sikh Guru, Guru Arjan Dev Ji, due to his devotion to Bhai Harnam Singh Ji. The original historic structure has largely been altered over time"),
    ("Dharam Shala, Pehli Patshahi", None, "Sindh", ""),
]

KNOWLEDGE_DOMAINS = [
    "AI", "Particle Physics", "Information Security", "Geopolitics",
    "Health", "Future Tech", "Poetry", "Art", "Music", "Solar Panels",
    "Electricity", "Trees & Plants",
]

CONTENT_TYPES = [
    "Document", "Video", "Music File", "Image File", "Software Code",
    "Audio/Podcast", "Spreadsheet", "Presentation",
]

# Full ISO 3166-1 alpha-2 list (minus 8 uninhabited territories) — see
# module docstring. (alpha-2 code, common name)
COUNTRIES = [
    ("AF", "Afghanistan"), ("AX", "Åland Islands"), ("AL", "Albania"),
    ("DZ", "Algeria"), ("AS", "American Samoa"), ("AD", "Andorra"),
    ("AO", "Angola"), ("AI", "Anguilla"), ("AG", "Antigua and Barbuda"),
    ("AR", "Argentina"), ("AM", "Armenia"), ("AW", "Aruba"),
    ("AU", "Australia"), ("AT", "Austria"), ("AZ", "Azerbaijan"),
    ("BS", "Bahamas"), ("BH", "Bahrain"), ("BD", "Bangladesh"),
    ("BB", "Barbados"), ("BY", "Belarus"), ("BE", "Belgium"),
    ("BZ", "Belize"), ("BJ", "Benin"), ("BM", "Bermuda"), ("BT", "Bhutan"),
    ("BO", "Bolivia"), ("BQ", "Bonaire, Sint Eustatius and Saba"),
    ("BA", "Bosnia and Herzegovina"), ("BW", "Botswana"), ("BR", "Brazil"),
    ("BN", "Brunei"), ("BG", "Bulgaria"), ("BF", "Burkina Faso"),
    ("BI", "Burundi"), ("CV", "Cabo Verde"), ("KH", "Cambodia"),
    ("CM", "Cameroon"), ("CA", "Canada"), ("KY", "Cayman Islands"),
    ("CF", "Central African Republic"), ("TD", "Chad"), ("CL", "Chile"),
    ("CN", "China"), ("CX", "Christmas Island"),
    ("CC", "Cocos (Keeling) Islands"), ("CO", "Colombia"),
    ("KM", "Comoros"), ("CD", "Congo (Democratic Republic of the)"),
    ("CG", "Congo"), ("CK", "Cook Islands"), ("CR", "Costa Rica"),
    ("CI", "Côte d'Ivoire"), ("HR", "Croatia"), ("CU", "Cuba"),
    ("CW", "Curaçao"), ("CY", "Cyprus"), ("CZ", "Czechia"),
    ("DK", "Denmark"), ("DJ", "Djibouti"), ("DM", "Dominica"),
    ("DO", "Dominican Republic"), ("EC", "Ecuador"), ("EG", "Egypt"),
    ("SV", "El Salvador"), ("GQ", "Equatorial Guinea"), ("ER", "Eritrea"),
    ("EE", "Estonia"), ("SZ", "Eswatini"), ("ET", "Ethiopia"),
    ("FK", "Falkland Islands"), ("FO", "Faroe Islands"), ("FJ", "Fiji"),
    ("FI", "Finland"), ("FR", "France"), ("GF", "French Guiana"),
    ("PF", "French Polynesia"), ("GA", "Gabon"), ("GM", "Gambia"),
    ("GE", "Georgia"), ("DE", "Germany"), ("GH", "Ghana"),
    ("GI", "Gibraltar"), ("GR", "Greece"), ("GL", "Greenland"),
    ("GD", "Grenada"), ("GP", "Guadeloupe"), ("GU", "Guam"),
    ("GT", "Guatemala"), ("GG", "Guernsey"), ("GN", "Guinea"),
    ("GW", "Guinea-Bissau"), ("GY", "Guyana"), ("HT", "Haiti"),
    ("VA", "Vatican City"), ("HN", "Honduras"), ("HK", "Hong Kong"),
    ("HU", "Hungary"), ("IS", "Iceland"), ("IN", "India"),
    ("ID", "Indonesia"), ("IR", "Iran"), ("IQ", "Iraq"), ("IE", "Ireland"),
    ("IM", "Isle of Man"), ("IL", "Israel"), ("IT", "Italy"),
    ("JM", "Jamaica"), ("JP", "Japan"), ("JE", "Jersey"), ("JO", "Jordan"),
    ("KZ", "Kazakhstan"), ("KE", "Kenya"), ("KI", "Kiribati"),
    ("KP", "North Korea"), ("KR", "South Korea"), ("KW", "Kuwait"),
    ("KG", "Kyrgyzstan"), ("LA", "Laos"), ("LV", "Latvia"),
    ("LB", "Lebanon"), ("LS", "Lesotho"), ("LR", "Liberia"), ("LY", "Libya"),
    ("LI", "Liechtenstein"), ("LT", "Lithuania"), ("LU", "Luxembourg"),
    ("MO", "Macao"), ("MK", "North Macedonia"), ("MG", "Madagascar"),
    ("MW", "Malawi"), ("MY", "Malaysia"), ("MV", "Maldives"),
    ("ML", "Mali"), ("MT", "Malta"), ("MH", "Marshall Islands"),
    ("MQ", "Martinique"), ("MR", "Mauritania"), ("MU", "Mauritius"),
    ("YT", "Mayotte"), ("MX", "Mexico"), ("FM", "Micronesia"),
    ("MD", "Moldova"), ("MC", "Monaco"), ("MN", "Mongolia"),
    ("ME", "Montenegro"), ("MS", "Montserrat"), ("MA", "Morocco"),
    ("MZ", "Mozambique"), ("MM", "Myanmar"), ("NA", "Namibia"),
    ("NR", "Nauru"), ("NP", "Nepal"), ("NL", "Netherlands"),
    ("NC", "New Caledonia"), ("NZ", "New Zealand"), ("NI", "Nicaragua"),
    ("NE", "Niger"), ("NG", "Nigeria"), ("NU", "Niue"),
    ("NF", "Norfolk Island"), ("MP", "Northern Mariana Islands"),
    ("NO", "Norway"), ("OM", "Oman"), ("PK", "Pakistan"), ("PW", "Palau"),
    ("PS", "Palestine"), ("PA", "Panama"), ("PG", "Papua New Guinea"),
    ("PY", "Paraguay"), ("PE", "Peru"), ("PH", "Philippines"),
    ("PN", "Pitcairn"), ("PL", "Poland"), ("PT", "Portugal"),
    ("PR", "Puerto Rico"), ("QA", "Qatar"), ("RE", "Réunion"),
    ("RO", "Romania"), ("RU", "Russia"), ("RW", "Rwanda"),
    ("BL", "Saint Barthélemy"),
    ("SH", "Saint Helena, Ascension and Tristan da Cunha"),
    ("KN", "Saint Kitts and Nevis"), ("LC", "Saint Lucia"),
    ("MF", "Saint Martin"), ("PM", "Saint Pierre and Miquelon"),
    ("VC", "Saint Vincent and the Grenadines"), ("WS", "Samoa"),
    ("SM", "San Marino"), ("ST", "Sao Tome and Principe"),
    ("SA", "Saudi Arabia"), ("SN", "Senegal"), ("RS", "Serbia"),
    ("SC", "Seychelles"), ("SL", "Sierra Leone"), ("SG", "Singapore"),
    ("SX", "Sint Maarten"), ("SK", "Slovakia"), ("SI", "Slovenia"),
    ("SB", "Solomon Islands"), ("SO", "Somalia"), ("ZA", "South Africa"),
    ("SS", "South Sudan"), ("ES", "Spain"), ("LK", "Sri Lanka"),
    ("SD", "Sudan"), ("SR", "Suriname"), ("SE", "Sweden"),
    ("CH", "Switzerland"), ("SY", "Syria"), ("TW", "Taiwan"),
    ("TJ", "Tajikistan"), ("TZ", "Tanzania"), ("TH", "Thailand"),
    ("TL", "Timor-Leste"), ("TG", "Togo"), ("TK", "Tokelau"),
    ("TO", "Tonga"), ("TT", "Trinidad and Tobago"), ("TN", "Tunisia"),
    ("TR", "Türkiye"), ("TM", "Turkmenistan"),
    ("TC", "Turks and Caicos Islands"), ("TV", "Tuvalu"), ("UG", "Uganda"),
    ("UA", "Ukraine"), ("AE", "United Arab Emirates"),
    ("GB", "United Kingdom"), ("US", "United States"), ("UY", "Uruguay"),
    ("UZ", "Uzbekistan"), ("VU", "Vanuatu"), ("VE", "Venezuela"),
    ("VN", "Vietnam"), ("VG", "British Virgin Islands"),
    ("VI", "U.S. Virgin Islands"), ("WF", "Wallis and Futuna"),
    ("EH", "Western Sahara"), ("YE", "Yemen"), ("ZM", "Zambia"),
    ("ZW", "Zimbabwe"),
]

# (code, full name) — label is always the full name, never just the code,
# so US states display consistently with every other country's provinces
# (e.g. Pakistan's, which have no code at all — see PAKISTAN_PROVINCES).
US_STATES = [
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
    ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"),
    ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"),
    ("IL", "Illinois"), ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"),
    ("KY", "Kentucky"), ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"), ("MS", "Mississippi"),
    ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
    ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"), ("OK", "Oklahoma"),
    ("OR", "Oregon"), ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"),
    ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"),
    ("VT", "Vermont"), ("VA", "Virginia"), ("WA", "Washington"), ("WV", "West Virginia"),
    ("WI", "Wisconsin"), ("WY", "Wyoming"), ("DC", "District of Columbia"),
]

CANADA_PROVINCES = [
    ("AB", "Alberta"), ("BC", "British Columbia"), ("MB", "Manitoba"),
    ("NB", "New Brunswick"), ("NL", "Newfoundland and Labrador"),
    ("NS", "Nova Scotia"), ("NT", "Northwest Territories"), ("NU", "Nunavut"),
    ("ON", "Ontario"), ("PE", "Prince Edward Island"), ("QC", "Quebec"),
    ("SK", "Saskatchewan"), ("YT", "Yukon"),
]

# GLOBAL — continent/region tier, sits above countries.
REGIONS = [
    ("ASIA", "Asia"), ("EUROPE", "Europe"), ("AFRICA", "Africa"),
    ("NORTH_AMERICA", "North America"), ("SOUTH_AMERICA", "South America"),
    ("OCEANIA", "Oceania"),
]

# Every one of the 241 seeded countries mapped to one of the REGIONS codes
# above (verified programmatically against the COUNTRIES list — no code is
# missing and none is assigned twice). A handful of transcontinental
# countries (Russia, Türkiye, Kazakhstan, Georgia, Cyprus, Egypt's Sinai,
# etc.) are placed by common convention rather than strict landmass split.
COUNTRY_REGIONS = {
    "AF": "ASIA", "AX": "EUROPE", "AL": "EUROPE", "DZ": "AFRICA",
    "AS": "OCEANIA", "AD": "EUROPE", "AO": "AFRICA", "AI": "NORTH_AMERICA",
    "AG": "NORTH_AMERICA", "AR": "SOUTH_AMERICA", "AM": "ASIA", "AW": "NORTH_AMERICA",
    "AU": "OCEANIA", "AT": "EUROPE", "AZ": "ASIA", "BS": "NORTH_AMERICA",
    "BH": "ASIA", "BD": "ASIA", "BB": "NORTH_AMERICA", "BY": "EUROPE",
    "BE": "EUROPE", "BZ": "NORTH_AMERICA", "BJ": "AFRICA", "BM": "NORTH_AMERICA",
    "BT": "ASIA", "BO": "SOUTH_AMERICA", "BQ": "NORTH_AMERICA", "BA": "EUROPE",
    "BW": "AFRICA", "BR": "SOUTH_AMERICA", "BN": "ASIA", "BG": "EUROPE",
    "BF": "AFRICA", "BI": "AFRICA", "CV": "AFRICA", "KH": "ASIA",
    "CM": "AFRICA", "CA": "NORTH_AMERICA", "KY": "NORTH_AMERICA", "CF": "AFRICA",
    "TD": "AFRICA", "CL": "SOUTH_AMERICA", "CN": "ASIA", "CX": "OCEANIA",
    "CC": "OCEANIA", "CO": "SOUTH_AMERICA", "KM": "AFRICA", "CD": "AFRICA",
    "CG": "AFRICA", "CK": "OCEANIA", "CR": "NORTH_AMERICA", "CI": "AFRICA",
    "HR": "EUROPE", "CU": "NORTH_AMERICA", "CW": "NORTH_AMERICA", "CY": "ASIA",
    "CZ": "EUROPE", "DK": "EUROPE", "DJ": "AFRICA", "DM": "NORTH_AMERICA",
    "DO": "NORTH_AMERICA", "EC": "SOUTH_AMERICA", "EG": "AFRICA", "SV": "NORTH_AMERICA",
    "GQ": "AFRICA", "ER": "AFRICA", "EE": "EUROPE", "SZ": "AFRICA",
    "ET": "AFRICA", "FK": "SOUTH_AMERICA", "FO": "EUROPE", "FJ": "OCEANIA",
    "FI": "EUROPE", "FR": "EUROPE", "GF": "SOUTH_AMERICA", "PF": "OCEANIA",
    "GA": "AFRICA", "GM": "AFRICA", "GE": "ASIA", "DE": "EUROPE",
    "GH": "AFRICA", "GI": "EUROPE", "GR": "EUROPE", "GL": "NORTH_AMERICA",
    "GD": "NORTH_AMERICA", "GP": "NORTH_AMERICA", "GU": "OCEANIA", "GT": "NORTH_AMERICA",
    "GG": "EUROPE", "GN": "AFRICA", "GW": "AFRICA", "GY": "SOUTH_AMERICA",
    "HT": "NORTH_AMERICA", "VA": "EUROPE", "HN": "NORTH_AMERICA", "HK": "ASIA",
    "HU": "EUROPE", "IS": "EUROPE", "IN": "ASIA", "ID": "ASIA",
    "IR": "ASIA", "IQ": "ASIA", "IE": "EUROPE", "IM": "EUROPE",
    "IL": "ASIA", "IT": "EUROPE", "JM": "NORTH_AMERICA", "JP": "ASIA",
    "JE": "EUROPE", "JO": "ASIA", "KZ": "ASIA", "KE": "AFRICA",
    "KI": "OCEANIA", "KP": "ASIA", "KR": "ASIA", "KW": "ASIA",
    "KG": "ASIA", "LA": "ASIA", "LV": "EUROPE", "LB": "ASIA",
    "LS": "AFRICA", "LR": "AFRICA", "LY": "AFRICA", "LI": "EUROPE",
    "LT": "EUROPE", "LU": "EUROPE", "MO": "ASIA", "MK": "EUROPE",
    "MG": "AFRICA", "MW": "AFRICA", "MY": "ASIA", "MV": "ASIA",
    "ML": "AFRICA", "MT": "EUROPE", "MH": "OCEANIA", "MQ": "NORTH_AMERICA",
    "MR": "AFRICA", "MU": "AFRICA", "YT": "AFRICA", "MX": "NORTH_AMERICA",
    "FM": "OCEANIA", "MD": "EUROPE", "MC": "EUROPE", "MN": "ASIA",
    "ME": "EUROPE", "MS": "NORTH_AMERICA", "MA": "AFRICA", "MZ": "AFRICA",
    "MM": "ASIA", "NA": "AFRICA", "NR": "OCEANIA", "NP": "ASIA",
    "NL": "EUROPE", "NC": "OCEANIA", "NZ": "OCEANIA", "NI": "NORTH_AMERICA",
    "NE": "AFRICA", "NG": "AFRICA", "NU": "OCEANIA", "NF": "OCEANIA",
    "MP": "OCEANIA", "NO": "EUROPE", "OM": "ASIA", "PK": "ASIA",
    "PW": "OCEANIA", "PS": "ASIA", "PA": "NORTH_AMERICA", "PG": "OCEANIA",
    "PY": "SOUTH_AMERICA", "PE": "SOUTH_AMERICA", "PH": "ASIA", "PN": "OCEANIA",
    "PL": "EUROPE", "PT": "EUROPE", "PR": "NORTH_AMERICA", "QA": "ASIA",
    "RE": "AFRICA", "RO": "EUROPE", "RU": "EUROPE", "RW": "AFRICA",
    "BL": "NORTH_AMERICA", "SH": "AFRICA", "KN": "NORTH_AMERICA", "LC": "NORTH_AMERICA",
    "MF": "NORTH_AMERICA", "PM": "NORTH_AMERICA", "VC": "NORTH_AMERICA", "WS": "OCEANIA",
    "SM": "EUROPE", "ST": "AFRICA", "SA": "ASIA", "SN": "AFRICA",
    "RS": "EUROPE", "SC": "AFRICA", "SL": "AFRICA", "SG": "ASIA",
    "SX": "NORTH_AMERICA", "SK": "EUROPE", "SI": "EUROPE", "SB": "OCEANIA",
    "SO": "AFRICA", "ZA": "AFRICA", "SS": "AFRICA", "ES": "EUROPE",
    "LK": "ASIA", "SD": "AFRICA", "SR": "SOUTH_AMERICA", "SE": "EUROPE",
    "CH": "EUROPE", "SY": "ASIA", "TW": "ASIA", "TJ": "ASIA",
    "TZ": "AFRICA", "TH": "ASIA", "TL": "ASIA", "TG": "AFRICA",
    "TK": "OCEANIA", "TO": "OCEANIA", "TT": "NORTH_AMERICA", "TN": "AFRICA",
    "TR": "ASIA", "TM": "ASIA", "TC": "NORTH_AMERICA", "TV": "OCEANIA",
    "UG": "AFRICA", "UA": "EUROPE", "AE": "ASIA", "GB": "EUROPE",
    "US": "NORTH_AMERICA", "UY": "SOUTH_AMERICA", "UZ": "ASIA", "VU": "OCEANIA",
    "VE": "SOUTH_AMERICA", "VN": "ASIA", "VG": "NORTH_AMERICA", "VI": "NORTH_AMERICA",
    "WF": "OCEANIA", "EH": "AFRICA", "YE": "ASIA", "ZM": "AFRICA",
    "ZW": "AFRICA",
}

# Pakistan provinces/cities — test seed data for the geography hierarchy
# feature, per the project brief's explicit example (Asia -> Pakistan ->
# Province -> City). Pakistan's provinces have no code (code = NULL), same
# convention as India's states — only the label is used.
#
# The first 6 are exactly the provinces named in the brief. "Gilgit
# Baltisitan" is seeded with no cities (none were supplied). "Azad Kashmir"
# is a 7th, EXTRA province not in the brief's list — added because the
# uploaded city/province CSV included real city data for it (Muzaffarabad,
# Mirpur); flagged here and in the delivery notes rather than silently
# dropped or silently added. The CSV spelled the Baluchistan province
# "Balochistan" — its cities are seeded under the brief's spelling,
# "Baluchistan", below.
PAKISTAN_PROVINCES = [
    "Punjab", "Sindh", "Baluchistan", "Khyber Pakhtunkhwa",
    "Gilgit Baltisitan", "Islamabad Capital Territory", "Azad Kashmir",
]

# Cleaned from the uploaded PakistanCityStateBook1.csv (127 rows): fixed
# mangled encoding ("K?moke" -> "Kamoke", "Nawabshah�(Benazirabad)" ->
# "Nawabshah (Benazirabad)") and the stray leading byte on province names.
#
# Extended with cities from LIST_OF_ALL_SIKH_GURDWARAS_IN_PAKISTAN1a.docx
# (see GURDWARA_POIS below) that weren't already in the CSV list. A few of
# that doc's own city/province pairings were corrected against real-world
# geography rather than taken as given, to avoid seeding a duplicate,
# wrongly-placed city: "Attock" and "Dera Ismail Khan" already existed here
# under their correct provinces (Punjab and Khyber Pakhtunkhwa
# respectively) even though the Gurdwara doc listed them under the other
# one; "Sakhi Sarwar" (Dera Ghazi Khan district) was listed under Sindh in
# the doc but is actually in Punjab; "Balakot" was listed under both KP and
# "Northern Areas" in the doc for different entries — merged into one
# Khyber Pakhtunkhwa city (its real location; "Northern Areas" was the
# pre-2009 name for Gilgit-Baltistan, so that entry looks like a doc typo).
# "Sakardu" (Skardu) is the one genuine new Gilgit-Baltistan city.
PAKISTAN_CITIES = {
    "Azad Kashmir": ["Muzaffarabad", "Mirpur"],
    "Baluchistan": ["Quetta", "Turbat", "Khuzdar", "Hub", "Panjgur", "Chaman", "Pishin",
        "Dera Murad Jamali", "Kallat"],
    "Khyber Pakhtunkhwa": ["Peshawar", "Mardan", "Mingora", "Kohat", "Abbottabad",
        "Dera Ismail Khan", "Swabi", "Mansehra", "Kabal", "Nowshera", "Charsadda", "Barikot",
        "Shabqadar", "Balakot", "Bannu", "Dhamial", "Naralil", "Rakh Topi"],
    "Punjab": ["Lahore", "Faisalabad", "Rawalpindi", "Gujranwala", "Multan", "Sargodha",
        "Sialkot", "Bahawalpur", "Jhang", "Sheikhupura", "Gujrat", "Sahiwal", "Okara",
        "Rahim Yar Khan", "Kasur", "Dera Ghazi Khan", "Wah Cantonment", "Burewala", "Hafizabad",
        "Chiniot", "Jhelum", "Kamoke", "Khanewal", "Sadiqabad", "Muridke", "Khanpur",
        "Bahawalnagar", "Muzaffargarh", "Mandi Bahauddin", "Daska", "Pakpattan", "Chakwal",
        "Gojra", "Vehari", "Ahmedpur East", "Chishtian", "Samundri", "Ferozwala", "Attock",
        "Jaranwala", "Hasilpur", "Kamalia", "Kot Abdul Malik", "Arif Wala",
        "Gujranwala Cantonment", "Jampur", "Jatoi", "Wazirabad", "Layyah", "Shujabad",
        "Haroonabad", "Jalalpur Jattan", "Lodhran", "Kot Addu", "Mian Channu", "Khushab",
        "Rajanpur", "Taxila", "Bhakkar", "Narowal", "Mianwali", "Shakargarh", "Mailsi",
        "Dipalpur", "Haveli Lakha", "Lala Musa", "Sambrial", "Bhalwal", "Taunsa", "Phool Nagar",
        "Pattoki", "Jauharabad", "Chichawatni", "Farooqabad", "Sangla Hill", "Gujar Khan",
        "Kharian", "Pasrur", "Kot Radha Kishan", "Ludhewala Waraich", "Renala Khurd",
        "Beharwal", "Bucheki", "Chak Ram Das", "Eimenabad", "Fateh Bhinder", "Harrapa",
        "Hassan Abdal", "Jaesukhwala", "Kachha", "Kartarpur", "Kaur", "Nankana Sahib", "Pohran",
        "Pusrur", "Rasool Nagar", "Rohtas", "Sakhi Sarwar", "Seoke", "Uch Sharif"],
    "Sindh": ["Karachi", "Hyderabad", "Sukkur", "Larkana", "Nawabshah (Benazirabad)",
        "Mirpur Khas", "Jacobabad", "Shikarpur", "Khairpur", "Dadu", "Tando Adam",
        "Tando Allahyar", "Bholari", "Umerkot", "Moro", "Shahdadkot", "Ghotki", "Badin",
        "Tando Muhammad Khan", "Shahdadpur", "Kamber Ali Khan", "Kotri", "Kandhkot"],
    "Islamabad Capital Territory": ["Islamabad"],
    "Gilgit Baltisitan": ["Sakardu"],
}

# Country calling codes (source: Wikipedia "List of country calling
# codes", ITU-T E.164 numbering plan). NANP members are stored as
# "+1-NNN" (area code) since bare "+1" doesn't distinguish them; a few
# dependent territories share their parent's numbering plan — see module
# docstring.
COUNTRY_CALLING_CODES = {
    "US": "+1", "CA": "+1", "BS": "+1-242", "BB": "+1-246", "AI": "+1-264",
    "AG": "+1-268", "VG": "+1-284", "KY": "+1-345", "BM": "+1-441",
    "GD": "+1-473", "TC": "+1-649", "JM": "+1-876", "MP": "+1-670",
    "GU": "+1-671", "AS": "+1-684", "SX": "+1-721", "LC": "+1-758",
    "DM": "+1-767", "VC": "+1-784", "DO": "+1-809", "TT": "+1-868",
    "KN": "+1-869", "VI": "+1-340", "MS": "+1-664", "PR": "+1-787",
    "EG": "+20", "SS": "+211", "MA": "+212", "EH": "+212", "DZ": "+213",
    "TN": "+216", "LY": "+218", "GM": "+220", "SN": "+221", "MR": "+222",
    "ML": "+223", "GN": "+224", "CI": "+225", "BF": "+226", "NE": "+227",
    "TG": "+228", "BJ": "+229", "MU": "+230", "LR": "+231", "SL": "+232",
    "GH": "+233", "NG": "+234", "TD": "+235", "CF": "+236", "CM": "+237",
    "CV": "+238", "ST": "+239", "GQ": "+240", "GA": "+241", "CG": "+242", "CD": "+243",
    "AO": "+244", "GW": "+245", "SC": "+248", "SD": "+249", "RW": "+250",
    "ET": "+251", "SO": "+252", "DJ": "+253", "KE": "+254", "TZ": "+255",
    "UG": "+256", "BI": "+257", "MZ": "+258", "ZM": "+260", "MG": "+261",
    "RE": "+262", "YT": "+262", "ZW": "+263", "NA": "+264", "MW": "+265",
    "LS": "+266", "BW": "+267", "SZ": "+268", "KM": "+269", "ZA": "+27",
    "SH": "+290", "ER": "+291", "AW": "+297", "FO": "+298", "GL": "+299",
    "GR": "+30", "NL": "+31", "BE": "+32", "FR": "+33", "ES": "+34",
    "GI": "+350", "PT": "+351", "LU": "+352", "IE": "+353", "IS": "+354",
    "AL": "+355", "MT": "+356", "CY": "+357", "FI": "+358", "AX": "+358",
    "BG": "+359", "HU": "+36", "LT": "+370", "LV": "+371", "EE": "+372",
    "MD": "+373", "AM": "+374", "BY": "+375", "AD": "+376", "MC": "+377",
    "SM": "+378", "VA": "+379", "UA": "+380", "RS": "+381", "ME": "+382",
    "HR": "+385", "SI": "+386", "BA": "+387", "MK": "+389", "IT": "+39",
    "RO": "+40", "CH": "+41", "CZ": "+420", "SK": "+421", "LI": "+423",
    "AT": "+43", "GB": "+44", "GG": "+44", "JE": "+44", "IM": "+44",
    "DK": "+45", "SE": "+46", "NO": "+47", "PL": "+48", "DE": "+49",
    "FK": "+500", "BZ": "+501", "GT": "+502", "SV": "+503", "HN": "+504",
    "NI": "+505", "CR": "+506", "PA": "+507", "PM": "+508", "HT": "+509",
    "PE": "+51", "MX": "+52", "CU": "+53", "AR": "+54", "BR": "+55",
    "CL": "+56", "CO": "+57", "VE": "+58", "GP": "+590", "BL": "+590",
    "MF": "+590", "BO": "+591", "GY": "+592", "EC": "+593", "GF": "+594",
    "PY": "+595", "MQ": "+596", "SR": "+597", "UY": "+598", "CW": "+599",
    "BQ": "+599", "MY": "+60", "AU": "+61", "CC": "+61", "CX": "+61",
    "ID": "+62", "PH": "+63", "NZ": "+64", "PN": "+64", "SG": "+65",
    "TH": "+66", "TL": "+670", "BN": "+673", "NR": "+674", "PG": "+675",
    "TO": "+676", "SB": "+677", "VU": "+678", "FJ": "+679", "PW": "+680",
    "WF": "+681", "CK": "+682", "NU": "+683", "WS": "+685", "KI": "+686",
    "NC": "+687", "TV": "+688", "PF": "+689", "TK": "+690", "FM": "+691",
    "MH": "+692", "NF": "+672", "RU": "+7", "KZ": "+7", "JP": "+81", "KR": "+82",
    "VN": "+84", "KP": "+850", "HK": "+852", "MO": "+853", "KH": "+855",
    "LA": "+856", "CN": "+86", "BD": "+880", "TW": "+886", "TR": "+90",
    "IN": "+91", "PK": "+92", "AF": "+93",
    "LK": "+94", "MM": "+95", "MV": "+960", "LB": "+961", "JO": "+962",
    "SY": "+963", "IQ": "+964", "KW": "+965", "SA": "+966", "YE": "+967",
    "OM": "+968", "PS": "+970", "AE": "+971", "IL": "+972", "BH": "+973",
    "QA": "+974", "BT": "+975", "MN": "+976", "NP": "+977", "IR": "+98",
    "TJ": "+992", "TM": "+993", "AZ": "+994", "GE": "+995", "KG": "+996",
    "UZ": "+998",
}


def _slug(label: str) -> str:
    return label.upper().replace("&", "AND").replace("/", "_").replace(" ", "_").replace("'", "").replace(".", "").replace("-", "_").replace(",", "")[:40]


def _seed_nested(db, table: str, parent_fk: str, tenant_id: int, parent_id: int, parent_code: str, labels):
    """Seed one level of a nested lookup table (e.g. supplier_subtypes under
    one specific supplier_types row) for a specific tenant. The generated
    code is prefixed with the parent's own code (e.g. "HOTEL_FIVE_STAR",
    not just "FIVE_STAR") — schema.sql's UNIQUE constraint on these tables
    is (tenant_id, code), not (tenant_id, parent, code), same as
    knowledge_subdomains/content_subtypes elsewhere in this schema, so an
    unprefixed code would silently collide (and get dropped by INSERT OR
    IGNORE) the moment two different parents share a sub-type label — which
    the supplier sub-type seed data below actually does ("Five Star" under
    both Hotel and Resort; "Other" under both Travel Agent and Airlines)."""
    for i, label in enumerate(labels):
        code = _slug(f"{parent_code}_{label}")
        db.execute(
            f"INSERT OR IGNORE INTO {table} (tenant_id, {parent_fk}, code, label, sort_order, is_active) VALUES (?, ?, ?, ?, ?, 1)",
            (tenant_id, parent_id, code, label, i),
        )


def seed_supplier_lookups(db, tenant_id: int):
    """Seeds supplier_types + supplier_subtypes (nested) for one tenant.
    Split out from seed_lookup_tables()'s flat _seed_simple calls because
    the sub-types need each type's row id/code, which only exists once the
    types themselves are committed."""
    _seed_simple(db, "supplier_types", tenant_id, SUPPLIER_TYPES)
    db.commit()
    for type_label, subtype_labels in SUPPLIER_SUBTYPES.items():
        parent = db.execute(
            "SELECT supplier_type_id, code FROM supplier_types WHERE tenant_id = ? AND label = ?",
            (tenant_id, type_label),
        ).fetchone()
        if not parent:
            continue
        _seed_nested(
            db, "supplier_subtypes", "supplier_type_id", tenant_id,
            parent["supplier_type_id"], parent["code"], subtype_labels,
        )
    db.commit()


def _seed_simple(db, table: str, tenant_id: int, labels):
    """Seed one TENANT-SCOPED lookup table for a specific tenant. Keyed on
    (tenant_id, code) — see schema.sql's per-tenant UNIQUE constraint on
    these tables — so re-running this for a tenant that already has its own
    (possibly since-edited) rows does not clobber anything."""
    for i, label in enumerate(labels):
        code = _slug(label)
        db.execute(
            f"INSERT OR IGNORE INTO {table} (tenant_id, code, label, sort_order, is_active) VALUES (?, ?, ?, ?, 1)",
            (tenant_id, code, label, i),
        )


def _seed_states(db, country_code, entries):
    """states is GLOBAL (no tenant_id) — real-world geography, shared by
    every tenant. Safe to call once ever; INSERT OR IGNORE makes repeat
    calls (e.g. from seeding a second tenant) no-ops.

    `entries` is a list of (code, label) tuples — code is None for
    countries whose provinces have no standard code (e.g. Pakistan). Label
    is always the full name, never just a code, so display is consistent
    across every country."""
    row = db.execute("SELECT country_id FROM countries WHERE code = ?", (country_code,)).fetchone()
    if not row:
        return
    for i, (code, label) in enumerate(entries):
        if code is None:
            # No `code` to key an OR IGNORE off — guard with a manual
            # existence check instead, scoped by (country_id, label).
            exists = db.execute(
                "SELECT 1 FROM states WHERE country_id = ? AND code IS NULL AND label = ?",
                (row["country_id"], label),
            ).fetchone()
            if exists:
                continue
            db.execute(
                "INSERT INTO states (country_id, code, label, sort_order, is_active) VALUES (?, NULL, ?, ?, 1)",
                (row["country_id"], label, i),
            )
        else:
            db.execute(
                "INSERT OR IGNORE INTO states (country_id, code, label, sort_order, is_active) VALUES (?, ?, ?, ?, 1)",
                (row["country_id"], code, label, i),
            )


def _seed_cities(db, country_code, province_label, city_labels):
    """cities is GLOBAL (no tenant_id), FK to states. Safe to re-run —
    manual existence check per (state_id, label) since there's no unique
    code to key an OR IGNORE off."""
    state = db.execute(
        "SELECT s.state_id FROM states s JOIN countries c ON c.country_id = s.country_id "
        "WHERE c.code = ? AND s.label = ?",
        (country_code, province_label),
    ).fetchone()
    if not state:
        return
    for i, label in enumerate(city_labels):
        exists = db.execute(
            "SELECT 1 FROM cities WHERE state_id = ? AND label = ?", (state["state_id"], label)
        ).fetchone()
        if exists:
            continue
        db.execute(
            "INSERT INTO cities (state_id, label, sort_order, is_active) VALUES (?, ?, ?, 1)",
            (state["state_id"], label, i),
        )


def seed_pakistan_geography(db):
    """Provinces + cities for Pakistan, per the project brief's test seed
    data (see PAKISTAN_PROVINCES/PAKISTAN_CITIES docstrings above for the
    Azad Kashmir / Baluchistan spelling notes). Safe to re-run."""
    _seed_states(db, "PK", [(None, label) for label in PAKISTAN_PROVINCES])
    for province_label, city_labels in PAKISTAN_CITIES.items():
        _seed_cities(db, "PK", province_label, city_labels)


def seed_gurdwara_pois(db, tenant_id: int):
    """Seeds the 149 Sikh Gurdwara points of interest (GURDWARA_POIS) for
    one tenant, per the request: Region defaults to Asia, Country defaults
    to Pakistan, and Province/City are resolved against the geography
    tables (seed_global_lookups() / seed_pakistan_geography() must have
    already run so those rows exist). This is Heritage-Tours-specific demo
    data, not a default for every future tenant, so it's called from
    seed_first_tenant() rather than the generic seed_lookup_tables().

    Idempotent: keyed on (tenant_id, name, city_id) via a manual existence
    check, since points_of_interest has no natural unique code to key an
    INSERT OR IGNORE off."""
    poi_type = db.execute(
        "SELECT poi_type_id FROM poi_types WHERE tenant_id = ? AND label = ?",
        (tenant_id, "Sikh Gurdwara"),
    ).fetchone()
    region = db.execute("SELECT region_id FROM regions WHERE code = ?", ("ASIA",)).fetchone()
    country = db.execute("SELECT country_id FROM countries WHERE code = ?", ("PK",)).fetchone()
    if not poi_type or not region or not country:
        return

    for name, city_label, province_label, notes in GURDWARA_POIS:
        state_id = None
        city_id = None
        state = db.execute(
            "SELECT state_id FROM states WHERE country_id = ? AND label = ?",
            (country["country_id"], province_label),
        ).fetchone()
        if state:
            state_id = state["state_id"]
            if city_label:
                city = db.execute(
                    "SELECT city_id FROM cities WHERE state_id = ? AND label = ?",
                    (state_id, city_label),
                ).fetchone()
                if city:
                    city_id = city["city_id"]

        exists = db.execute(
            "SELECT 1 FROM points_of_interest WHERE tenant_id = ? AND name = ? AND city_id IS ?",
            (tenant_id, name, city_id),
        ).fetchone()
        if exists:
            continue

        db.execute(
            "INSERT INTO points_of_interest "
            "(tenant_id, name, poi_type_id, region_id, country_id, state_id, city_id, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, name, poi_type["poi_type_id"], region["region_id"],
             country["country_id"], state_id, city_id, notes),
        )


# ---------------------------------------------------------------------------
# "Sikh Pilgrimage Sector" AI Agent / Data Enrichment -- Phase 1 pilot city
# (Nankana Sahib, Punjab), Sept 2026. Per Zeb's request, before scaling
# across all 55 named cities + international hub cities, the pipeline
# (schema -> real data gathering -> DB population -> Knowledge Graph
# linking) is proven on one pilot city. Nankana Sahib was chosen: it is
# the birthplace of Guru Nanak Dev Ji and home to Gurdwara Janam Asthan,
# arguably Pakistan's most important Sikh Gurdwara; and it already has 9
# Gurdwara POI records seeded (GURDWARA_POIS above) with no coordinates,
# making it a genuine "enrich existing + add missing categories" exercise.
#
# All facts below were gathered via live web research (WebSearch/WebFetch),
# never fabricated from memory, per the standing anti-hallucination rule --
# each entry's notes field names its source and date. Where a precise,
# independently-published coordinate for one specific site could not be
# found, the town-center coordinate for Nankana Sahib itself is used as an
# honest approximation and flagged "approximate" in the notes -- rather
# than inventing false precision. This is exactly the kind of gap the
# eventual AI Agent is meant to keep closing as it gathers deeper data.
#
# Coordinates are stored as "lat, lon" decimal degrees (parsed by
# utils.parse_coordinates()/rendered by the map_coordinates_link filter --
# no schema change needed; see utils.py).
NANKANA_SAHIB_TOWN_COORDS = "31.450000, 73.706670"  # Source: Wikipedia "Nankana Sahib", Sept 2026

# (name, poi_type_label, city_label, province_label, map_coordinates, notes)
NANKANA_SAHIB_PILOT_POIS = [
    (
        "Quba Masjid, Nankana Sahib", "Mosque", "Nankana Sahib", "Punjab",
        NANKANA_SAHIB_TOWN_COORDS,
        "A replica of the Quba Masjid of Madina. Source: PC Hospitality "
        "blog, Sept 2026. Coordinates are the Nankana Sahib town-center "
        "reference point (Wikipedia) -- not an independently verified "
        "site-level coordinate.",
    ),
    (
        "Nankana Lake Resort", "Other", "Nankana Sahib", "Punjab",
        NANKANA_SAHIB_TOWN_COORDS,
        "Leisure destination (boating, gardens) near Nankana Sahib; ~90 km "
        "from Lahore per the existing Organization Intelligence entry on "
        "this same resort. Source: PC Hospitality blog, Sept 2026. "
        "Coordinates approximate (town-center reference).",
    ),
    (
        "Nankana Sahib Railway Station", "Railway Station", "Nankana Sahib", "Punjab",
        "31.455400, 73.709000",
        "On the Shorkot-Sheikhupura Branch Line, between Buchiana and "
        "Warburton stations. Source: Wikipedia \"Nankana Sahib railway "
        "station\", Sept 2026.",
    ),
    (
        "District Head Quarter (DHQ) Hospital, Nankana Sahib", "Hospital", "Nankana Sahib", "Punjab",
        NANKANA_SAHIB_TOWN_COORDS,
        "Hospital Rd, Nankana Sahib. Source: marham.pk hospital directory, "
        "Sept 2026. Coordinates approximate (town-center reference) -- "
        "exact site coordinates were not published on the source page.",
    ),
    (
        "Nankana Sahib City Police Station", "Police Station", "Nankana Sahib", "Punjab",
        NANKANA_SAHIB_TOWN_COORDS,
        "Listed in the Punjab Police Nankana Sahib district directory "
        "(punjabpolice.gov.pk/nankana_directory -- page blocked automated "
        "fetch at research time; confirm exact address before operational "
        "use). Coordinates approximate (town-center reference).",
    ),
    (
        "Allama Iqbal International Airport", "Airport", "Lahore", "Punjab",
        "31.519346, 74.409302",
        "Nearest major international airport to Nankana Sahib -- "
        "approx. 91 km / 57 mi east of the town (Wikipedia). Primary air "
        "gateway for this pilot; also the reference airport for the "
        "Kuala Lumpur/Singapore/Dubai/Abu Dhabi/Doha/Istanbul hub-city "
        "routing Zeb described. Source: Wikipedia \"Nankana Sahib\" + "
        "latlong.net, Sept 2026.",
    ),
]

# (supplier_name, supplier_type_label, supplier_subtype_label_or_None,
#  preference_or_None, city_label, province_label, web_page_or_None, notes)
NANKANA_SAHIB_PILOT_SUPPLIERS = [
    (
        "Hotel One Nankana Sahib", "Hotel", "Motel", "Primary",
        "Nankana Sahib", "Punjab", "https://www.pchotels.com",
        "TDCP-operated motel/resort close to Gurdwara Janam Asthan -- "
        "short local transport needed, per source. Purpose-positioned for "
        "pilgrim stays; marked Primary as the most established option "
        "found. Source: PC Hospitality blog, Sept 2026.",
    ),
    (
        "Rana Resort, Nankana Sahib", "Resort", None, "Secondary",
        "Nankana Sahib", "Punjab", None,
        "Family-friendly resort accommodation. Source: PC Hospitality "
        "blog, Sept 2026 -- star-tier not independently confirmed, left "
        "blank rather than guessed.",
    ),
    (
        "City Family Restaurant, Nankana Sahib", "Restaurant", "Punjabi", "Primary",
        "Nankana Sahib", "Punjab", None,
        "Local restaurant serving Punjabi fare, per its public business "
        "listing (Facebook), Sept 2026 -- address/menu not independently "
        "verified; confirm before operational use.",
    ),
    (
        "Sikh Tourism Pakistan", "Tour Operator", "Inbound", "Primary",
        "Lahore", "Punjab", "https://sikhtourism.com.pk/",
        "Operates Gurdwara Yatra / Sikh pilgrimage tour packages across "
        "Pakistan including Nankana Sahib (6 curated packages, "
        "$240-$485/pax, groups of 10, per source). Source: company "
        "website, Sept 2026. City set to Lahore as Pakistan's principal "
        "inbound-tourism hub -- the site does not publish an office "
        "address, so this is the operator's likely base, not a confirmed "
        "location; verify before contacting.",
    ),
    (
        "Trango Adventure", "Tour Operator", "Inbound", "Secondary",
        "Lahore", "Punjab", "https://trangoadventure.com/",
        "Also runs a Sikh Pilgrimage Tour Pakistan package (Kartarpur, "
        "Nankana Sahib, Panja Sahib). Source: company website (tour "
        "listing page), Sept 2026 -- office address blocked automated "
        "fetch; city is an assumption pending verification.",
    ),
]

# Knowledge Graph edges to draw once the POIs/Suppliers above exist --
# (subject_name, subject_type, relationship, object_name, object_type,
#  notes_or_None). Names are resolved against already-seeded rows (both
# the 9 GURDWARA_POIS and the two lists above); a lookup that finds
# nothing is skipped rather than raising, so this stays safe to extend.
NANKANA_SAHIB_KG_EDGES = [
    ("Gurdwara Janam Asthan, Nankana Sahib", "PointOfInterest", "near", "Hotel One Nankana Sahib", "Supplier",
     "Short local transport needed between the two, per source."),
    ("Gurdwara Bal Lila, Nankana Sahib", "PointOfInterest", "located_in", "Nankana Sahib", "City", None),
    ("Nankana Sahib Railway Station", "PointOfInterest", "serves", "Gurdwara Janam Asthan, Nankana Sahib", "PointOfInterest",
     "Nankana Sahib town's own rail link, on the Shorkot-Sheikhupura Branch Line."),
    ("Allama Iqbal International Airport", "PointOfInterest", "nearest_airport_to", "Gurdwara Janam Asthan, Nankana Sahib", "PointOfInterest",
     "~91 km / 57 mi from Nankana Sahib -- the practical air gateway for this pilot."),
    ("Sikh Tourism Pakistan", "Supplier", "operates_tours_to", "Gurdwara Janam Asthan, Nankana Sahib", "PointOfInterest", None),
    ("Trango Adventure", "Supplier", "operates_tours_to", "Gurdwara Janam Asthan, Nankana Sahib", "PointOfInterest", None),
    ("Hotel One Nankana Sahib", "Supplier", "located_in", "Nankana Sahib", "City", None),
    ("City Family Restaurant, Nankana Sahib", "Supplier", "located_in", "Nankana Sahib", "City", None),
]


def seed_nankana_sahib_pilot(db, tenant_id: int):
    """Populates the Nankana Sahib pilot-city enrichment: real coordinates
    on the 9 already-seeded Gurdwara POIs, the new non-Gurdwara POIs
    (NANKANA_SAHIB_PILOT_POIS), the new Suppliers
    (NANKANA_SAHIB_PILOT_SUPPLIERS) with a Main Office address each, and
    the Knowledge Graph edges linking them (NANKANA_SAHIB_KG_EDGES).
    Heritage-Tours-specific pilot data, same reasoning as
    seed_gurdwara_pois() -- called from seed_first_tenant() rather than
    seed_lookup_tables(). Requires seed_gurdwara_pois() and
    seed_supplier_lookups()/_seed_simple(poi_types) to already have run.
    Idempotent throughout."""
    from knowledge_graph import add_edge

    country = db.execute("SELECT country_id FROM countries WHERE code = ?", ("PK",)).fetchone()
    if not country:
        return
    nankana_state = db.execute(
        "SELECT state_id FROM states WHERE country_id = ? AND label = ?",
        (country["country_id"], "Punjab"),
    ).fetchone()
    if not nankana_state:
        return
    nankana_city = db.execute(
        "SELECT city_id FROM cities WHERE state_id = ? AND label = ?",
        (nankana_state["state_id"], "Nankana Sahib"),
    ).fetchone()
    if not nankana_city:
        return

    # 1) Backfill map_coordinates on the 9 existing Nankana-Sahib-tied
    # Gurdwara POI rows, only where still blank (never overwrite a value
    # someone has since edited by hand).
    db.execute(
        "UPDATE points_of_interest SET map_coordinates = ? "
        "WHERE tenant_id = ? AND city_id = ? AND (map_coordinates IS NULL OR map_coordinates = '')",
        (NANKANA_SAHIB_TOWN_COORDS, tenant_id, nankana_city["city_id"]),
    )

    # 2) New non-Gurdwara POIs.
    for name, poi_type_label, city_label, province_label, coords, notes in NANKANA_SAHIB_PILOT_POIS:
        poi_type = db.execute(
            "SELECT poi_type_id FROM poi_types WHERE tenant_id = ? AND label = ?",
            (tenant_id, poi_type_label),
        ).fetchone()
        state = db.execute(
            "SELECT state_id FROM states WHERE country_id = ? AND label = ?",
            (country["country_id"], province_label),
        ).fetchone()
        city = None
        if state:
            city = db.execute(
                "SELECT city_id FROM cities WHERE state_id = ? AND label = ?",
                (state["state_id"], city_label),
            ).fetchone()
        if not poi_type or not state or not city:
            continue
        exists = db.execute(
            "SELECT 1 FROM points_of_interest WHERE tenant_id = ? AND name = ? AND city_id = ?",
            (tenant_id, name, city["city_id"]),
        ).fetchone()
        if exists:
            continue
        db.execute(
            "INSERT INTO points_of_interest "
            "(tenant_id, name, poi_type_id, state_id, city_id, map_coordinates, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, name, poi_type["poi_type_id"], state["state_id"], city["city_id"], coords, notes),
        )

    # 3) New Suppliers (+ one Main Office address each).
    main_office = db.execute(
        "SELECT address_type_id FROM supplier_address_types WHERE tenant_id = ? AND label = ?",
        (tenant_id, "Main Office"),
    ).fetchone()
    for name, type_label, subtype_label, preference, city_label, province_label, web_page, notes in NANKANA_SAHIB_PILOT_SUPPLIERS:
        exists = db.execute(
            "SELECT supplier_id FROM suppliers WHERE tenant_id = ? AND supplier_name = ?",
            (tenant_id, name),
        ).fetchone()
        if exists:
            continue
        supplier_type = db.execute(
            "SELECT supplier_type_id FROM supplier_types WHERE tenant_id = ? AND label = ?",
            (tenant_id, type_label),
        ).fetchone()
        supplier_subtype = None
        if subtype_label and supplier_type:
            supplier_subtype = db.execute(
                "SELECT supplier_subtype_id FROM supplier_subtypes WHERE tenant_id = ? AND supplier_type_id = ? AND label = ?",
                (tenant_id, supplier_type["supplier_type_id"], subtype_label),
            ).fetchone()
        cur = db.execute(
            "INSERT INTO suppliers (tenant_id, supplier_name, supplier_type_id, supplier_subtype_id, "
            "web_page, notes, preference) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id, name,
                supplier_type["supplier_type_id"] if supplier_type else None,
                supplier_subtype["supplier_subtype_id"] if supplier_subtype else None,
                web_page, notes, preference,
            ),
        )
        supplier_id = cur.lastrowid

        state = db.execute(
            "SELECT state_id FROM states WHERE country_id = ? AND label = ?",
            (country["country_id"], province_label),
        ).fetchone()
        city = None
        if state:
            city = db.execute(
                "SELECT city_id FROM cities WHERE state_id = ? AND label = ?",
                (state["state_id"], city_label),
            ).fetchone()
        if state and city:
            db.execute(
                "INSERT INTO supplier_addresses "
                "(tenant_id, supplier_id, address_type_id, country_id, state_id, city_id, is_primary) "
                "VALUES (?, ?, ?, ?, ?, ?, 1)",
                (tenant_id, supplier_id,
                 main_office["address_type_id"] if main_office else None,
                 country["country_id"], state["state_id"], city["city_id"]),
            )

    db.commit()

    # 4) Knowledge Graph edges linking the above together.
    def _resolve(entity_type, name):
        if entity_type == "PointOfInterest":
            row = db.execute(
                "SELECT poi_id FROM points_of_interest WHERE tenant_id = ? AND name = ?",
                (tenant_id, name),
            ).fetchone()
            return row["poi_id"] if row else None
        if entity_type == "Supplier":
            row = db.execute(
                "SELECT supplier_id FROM suppliers WHERE tenant_id = ? AND supplier_name = ?",
                (tenant_id, name),
            ).fetchone()
            return row["supplier_id"] if row else None
        if entity_type == "City":
            row = db.execute("SELECT city_id FROM cities WHERE label = ?", (name,)).fetchone()
            return row["city_id"] if row else None
        return None

    for subj_name, subj_type, relationship, obj_name, obj_type, notes in NANKANA_SAHIB_KG_EDGES:
        subj_id = _resolve(subj_type, subj_name)
        obj_id = _resolve(obj_type, obj_name)
        if not subj_id or not obj_id:
            continue
        add_edge(db, tenant_id, subj_type, subj_id, relationship, obj_type, obj_id, notes)
    db.commit()


# Sample "Organization Intelligence" journal entries for Heritage Tours --
# real, dated operational knowledge already gathered by staff (place-name
# spelling variants, a hotel's pet/childcare policies, drive times and
# seasonal road conditions). Each is a (entry_date, note) pair; every entry
# in this batch shares the same entry_date, so on the Intelligence screen
# (newest entry_date first, ties broken by whichever was entered last) they
# surface in the reverse of this list's order -- the same "newest visible
# first" behavior real day-to-day use will have once entries start landing
# on different dates.
ORGANIZATION_INTELLIGENCE_SEED = [
    ("2026-08-26", "The term 'Gurdwara' and 'Gurudawara' is used interchangeably and means the same thing."),
    ("2026-08-26", "The term 'Janamastan' and 'Tanamashtan' is used interchangeably and means the same thing."),
    ("2026-08-26", "Avari Hotel in Islamabad does not allow Dogs but cats are Ok."),
    ("2026-08-26", "Avari Hotel Lahore does not have a Childcare facilty. 3rd Party Childcare may be available."),
    ("2026-08-26", "Nankana Resort is approx.. 90 Km from Lahore. They have a pick/drop service."),
    ("2026-08-26", "Gurdwara 'Punja Sahib' in Hasan Abdal is approx.. 7 Hours drive from Lahore in clear weather. "
                    "The Nearest overnight stay is practical in Islamabad and Rawalpindi."),
    ("2026-08-26", "A Day trip Murree Hills requires leaving at 9:00Am latest from Rawalpindi or Islamabad. Murre is "
                    "approximately 1.5 hours Drive on a normal day with good weather and normal traffic. In winter "
                    "with snow and traffic it can take upwards of 3 Hours. Also tire on the vehicles must have good "
                    "traction and Snow chains are recommended. People have been known to be stranded for hours in "
                    "Automobiles struck in snow. Accidents are common in Murree Hills during snow for un-prepared / "
                    "poorly equipped vehicles."),
]


def seed_organization_intelligence(db, tenant_id: int):
    """Seeds ORGANIZATION_INTELLIGENCE_SEED for one tenant, attributed to
    that tenant's 'Zeb' user when one exists (falling back to the plain
    name 'Zeb' as entered_by_name only, with no user_id, if it doesn't --
    keeps this usable against a tenant that isn't Heritage Tours without
    erroring). Heritage-Tours-specific demo data, not a default for every
    future tenant, so it's called from seed_first_tenant() rather than the
    generic seed_lookup_tables() -- same reasoning as seed_gurdwara_pois()
    above.

    Idempotent: no-ops if this tenant already has ANY organization_
    intelligence rows, so re-running seed-tenant (or the migration this
    also backs, for an existing database) never creates duplicates."""
    existing = db.execute(
        "SELECT 1 FROM organization_intelligence WHERE tenant_id = ? LIMIT 1", (tenant_id,)
    ).fetchone()
    if existing:
        return

    user = db.execute(
        "SELECT user_id, display_name FROM users WHERE tenant_id = ? AND username = 'Zeb'", (tenant_id,)
    ).fetchone()
    user_id = user["user_id"] if user else None
    entered_by_name = user["display_name"] if user else "Zeb"

    for entry_date, note in ORGANIZATION_INTELLIGENCE_SEED:
        db.execute(
            """INSERT INTO organization_intelligence
               (tenant_id, entry_date, entered_by_user_id, entered_by_name, note)
               VALUES (?, ?, ?, ?, ?)""",
            (tenant_id, entry_date, user_id, entered_by_name, note),
        )
    db.commit()


# ============================================================================
# Service Coding System (Inventory -> Services sub-module) -- GLOBAL
# taxonomy, per Service_Coding_System_Schema.md. Transcribed directly from
# the companion TTMS_Service_Coding_Seed.sql reference script (Categories
# -> Groups -> Sub-Groups -> validity flags), adapted to this app's SQLite
# schema (service_categories/service_groups/service_subgroups -- see
# schema.sql's MODULE I). Seeded once, globally, the same way regions/
# countries/states/cities are -- see seed_service_taxonomy() below.
# ============================================================================

# (category_code, category_name, description, is_active)
SERVICE_CATEGORIES = [
    ("TP", "Tour Package", "Multi-day and single-destination tour products.", True),
    ("GT", "Ground Transport", "Car and bus rental/charter. Canonical -- supersedes the retired TC/TB below.", True),
    ("RT", "Rail Transport", "All rail modes: intercity, high-speed, sleeper, scenic, charter, metro. Canonical -- supersedes the retired TR below.", True),
    ("AT", "Airline", "All air modes: scheduled, low-cost, charter, private jet, helicopter, seaplane. Canonical -- supersedes the retired TA below.", True),
    ("ST", "Shipping / Boat", "Ferry, cruise, river cruise, day cruise, speedboat, charter boat/yacht. Canonical -- supersedes the retired TS below.", True),
    ("AR", "Accommodation / Rooms", "Room/property booking -- not a hotel PMS.", True),
    ("FB", "Food and Beverages", "Restaurant, set/banquet, buffet, packed meals, culinary experiences.", True),
    ("GS", "Guide Services", "Local guide, tour leader, specialist guide, driver-guide, interpreter.", True),
    ("ET", "Entry Tickets / Permits", "Museum, park, religious site, trekking, NOC, and filming permits.", True),
    ("VP", "VISA Processing", "Tourist, business, transit, Umrah, e-visa, and group visa processing.", True),
    # Legacy categories, retired in favor of the structured GT/RT/AT/ST
    # Categories above (each of these was a flat, structureless top-level
    # Category with no Groups/Sub-Groups of its own) -- retained for
    # historical/audit reference only, never dropped.
    ("TC", "Transport (Car) [DEPRECATED]", "Deprecated -- superseded by GT (Ground Transport: Car Rental).", False),
    ("TB", "Transport (Bus) [DEPRECATED]", "Deprecated -- superseded by GT (Ground Transport: Bus/Coach Charter).", False),
    ("TR", "Transport (Rail) [DEPRECATED]", "Deprecated -- superseded by RT (Rail Transport).", False),
    ("TA", "Transport (Airline) [DEPRECATED]", "Deprecated -- superseded by AT (Airline).", False),
    ("TS", "Transport (Ship/Boat) [DEPRECATED]", "Deprecated -- superseded by ST (Shipping/Boat).", False),
]

# (category_code, group_code, group_name, description-or-None)
SERVICE_GROUPS = [
    ("TP", "MN", "Multi-Nation Tour", None),
    ("TP", "SN", "Single Nation Tour", None),
    ("TP", "DT", "Domestic Tour", None),

    ("GT", "CR", "Car Rental", None),
    ("GT", "BS", "Bus / Coach Charter", None),

    ("RT", "IC", "Intercity / Standard Rail", None),
    ("RT", "HS", "High-Speed Rail", None),
    ("RT", "SL", "Sleeper / Overnight Rail", None),
    ("RT", "SR", "Scenic / Heritage Rail", None),
    ("RT", "CH", "Private Charter Rail", None),
    ("RT", "MT", "Metro / Urban Transit", None),

    ("AT", "FS", "Full-Service Carrier", None),
    ("AT", "LC", "Low-Cost Carrier", None),
    ("AT", "CH", "Charter Flight", "Includes Hajj/Umrah and worker-corridor charters."),
    ("AT", "PJ", "Private Jet Charter", None),
    ("AT", "HC", "Helicopter Charter/Transfer", None),
    ("AT", "SC", "Seaplane / Scenic Air", None),

    ("ST", "FE", "Ferry", None),
    ("ST", "CR", "Ocean Cruise Line", None),
    ("ST", "RC", "River Cruise", None),
    ("ST", "DC", "Day Cruise / Excursion Boat", None),
    ("ST", "SP", "Speedboat / Water Taxi", None),
    ("ST", "CH", "Private Charter Boat/Yacht", None),

    ("AR", "HT", "Hotel", None),
    ("AR", "RS", "Resort", None),
    ("AR", "GH", "Guesthouse / B&B", None),
    ("AR", "SA", "Serviced Apartment", None),
    ("AR", "CG", "Camp / Glamping", None),
    ("AR", "HS", "Hostel", None),

    ("FB", "RM", "Restaurant Meal", None),
    ("FB", "SM", "Set / Banquet Meal", None),
    ("FB", "BF", "Buffet Meal", None),
    ("FB", "PN", "Packed / Trail Meal", None),
    ("FB", "CE", "Culinary Experience", None),

    ("GS", "LG", "Local Guide", None),
    ("GS", "TL", "Tour Leader / Escort", None),
    ("GS", "SG", "Specialist / Adventure Guide", None),
    ("GS", "DG", "Driver-Guide", None),
    ("GS", "IN", "Interpreter / Translator", None),

    ("ET", "MU", "Museum / Monument Entry", None),
    ("ET", "NP", "National Park / Nature Reserve", None),
    ("ET", "RS", "Religious Site Entry", None),
    ("ET", "TP", "Trekking / Mountaineering Permit", None),
    ("ET", "NC", "NOC / Restricted Area Permit", None),
    ("ET", "PF", "Photography / Filming Permit", None),

    ("VP", "TV", "Tourist Visa", None),
    ("VP", "BV", "Business Visa", None),
    ("VP", "TR", "Transit Visa", None),
    ("VP", "UV", "Umrah / Pilgrim Visa", None),
    ("VP", "EV", "e-Visa / VOA Facilitation", None),
    ("VP", "GV", "Group Visa", None),
]

# Sub-Group vocabularies -- most Groups within a Category share one set
# (e.g. every TP Group takes the same MC/SC/SD set); a few Groups get their
# own vocabulary instead (flagged in the schema doc: a Group whose natural
# variation isn't "class of seat/room/cabin" shouldn't be forced through a
# vocabulary built for classed travel/lodging). Each entry:
# (category_code, [group_codes sharing this set], [(subgroup_code, name), ...])
SERVICE_SUBGROUP_SETS = [
    ("TP", ["MN", "SN", "DT"], [
        ("MC", "Multi-City POIs"), ("SC", "Single-City POIs"), ("SD", "Single Destination (Single POI)"),
    ]),
    ("GT", ["CR", "BS"], [
        ("LN", "Luxury, No-Driver"), ("LD", "Luxury, With Driver"), ("EN", "Economy, No-Driver"),
        ("ED", "Economy, With Driver"), ("SN", "SUV, No-Driver"), ("SD", "SUV, With Driver"),
    ]),
    ("RT", ["IC", "HS", "SL", "SR"], [
        ("1C", "First / Business Class"), ("2C", "Second / Standard Class"), ("EC", "Economy Class"),
        ("PC", "Private Cabin / Sleeper Compartment"), ("PV", "Panoramic / Observation Car"),
    ]),
    ("RT", ["CH"], [("FT", "Full Train Charter"), ("PR", "Private Carriage Charter"), ("SP", "Special Event / Themed Charter")]),
    ("RT", ["MT"], [("ST", "Standard"), ("1C", "First Class")]),
    ("AT", ["FS", "LC", "CH"], [
        ("1C", "First Class"), ("BC", "Business Class"), ("PE", "Premium Economy"), ("EC", "Economy Class"),
    ]),
    ("AT", ["PJ"], [("LJ", "Light Jet"), ("MJ", "Midsize Jet"), ("HJ", "Heavy / Long-Range Jet")]),
    ("AT", ["HC", "SC"], [("SS", "Scenic/Sightseeing"), ("TR", "Point-to-Point Transfer"), ("PR", "Private Charter")]),
    ("ST", ["FE"], [("DK", "Deck / Open Seating"), ("ST", "Standard Seat"), ("CB", "Private Cabin")]),
    ("ST", ["CR", "RC"], [
        ("IN", "Interior Cabin"), ("EX", "Exterior / View Cabin"), ("BL", "Balcony / Veranda"), ("SU", "Suite"),
    ]),
    ("ST", ["DC"], [("ST", "Standard / General Admission"), ("PR", "Premium / VIP Section")]),
    ("ST", ["SP"], [("SH", "Shared / Scheduled"), ("PR", "Private Charter")]),
    ("ST", ["CH"], [("SB", "Small Boat/Speedboat"), ("YC", "Yacht Charter"), ("LB", "Large Traditional Vessel")]),
    ("AR", ["HT", "RS", "GH", "SA", "CG", "HS"], [
        ("ST", "Standard Room"), ("DL", "Deluxe Room"), ("SU", "Suite"), ("VL", "Villa"), ("DM", "Dormitory / Shared Room"),
    ]),
    ("FB", ["RM", "SM", "BF", "PN"], [
        ("BK", "Breakfast"), ("LN", "Lunch"), ("DN", "Dinner"), ("SN", "Snack / Tea Break"), ("FD", "Full-Day / All-Meals"),
    ]),
    ("FB", ["CE"], [("CC", "Cooking Class"), ("FT", "Food Tour/Tasting Walk"), ("TT", "Tea/Coffee Tasting"), ("WT", "Wine/Spirits Tasting")]),
    ("GS", ["LG", "TL", "SG", "DG", "IN"], [
        ("HR", "Hourly"), ("HD", "Half-Day"), ("FD", "Full-Day"), ("ML", "Multi-Day / Per Tour"),
    ]),
    ("ET", ["MU", "NP", "RS", "TP", "NC"], [
        ("FN", "Foreign National Rate"), ("LC", "Local/Citizen Rate"), ("ST", "Student/Concession Rate"), ("GP", "Group Rate"),
    ]),
    ("ET", ["PF"], [("PC", "Personal Camera/Phone"), ("CF", "Commercial Filming"), ("DR", "Drone/Aerial")]),
    ("VP", ["TV", "BV", "TR", "UV", "EV", "GV"], [
        ("STD", "Standard Processing"), ("EXP", "Express / Rush Processing"), ("SDS", "Same-Day / Super-Rush"),
    ]),
]

# Every validity_status exception -- everything not listed here defaults to
# 'active' (set when the row is first created, per SERVICE_SUBGROUP_SETS
# above). (category_code, group_code, subgroup_code, validity_status).
# Transcribed 1:1 from TTMS_Service_Coding_Seed.sql section 4.
SERVICE_SUBGROUP_OVERRIDES = [
    ("TP", "MN", "SD", "deprecated"),

    ("GT", "BS", "LN", "deprecated"), ("GT", "BS", "EN", "deprecated"),
    ("GT", "BS", "SN", "deprecated"), ("GT", "BS", "SD", "deprecated"),

    ("RT", "IC", "PC", "deprecated"), ("RT", "IC", "PV", "deprecated"),
    ("RT", "HS", "PC", "under_review"), ("RT", "HS", "PV", "deprecated"),
    ("RT", "SL", "EC", "deprecated"), ("RT", "SL", "PV", "deprecated"),
    ("RT", "SR", "2C", "deprecated"),

    ("AT", "LC", "1C", "deprecated"), ("AT", "LC", "PE", "deprecated"), ("AT", "LC", "BC", "under_review"),
    ("AT", "CH", "1C", "deprecated"), ("AT", "CH", "PE", "deprecated"),

    ("ST", "RC", "IN", "under_review"),

    ("AR", "HT", "VL", "under_review"), ("AR", "HT", "DM", "deprecated"),
    ("AR", "RS", "DM", "deprecated"),
    ("AR", "GH", "DL", "under_review"), ("AR", "GH", "SU", "under_review"),
    ("AR", "GH", "VL", "deprecated"), ("AR", "GH", "DM", "deprecated"),
    ("AR", "SA", "VL", "under_review"), ("AR", "SA", "DM", "deprecated"),
    ("AR", "CG", "SU", "under_review"), ("AR", "CG", "VL", "under_review"),
    ("AR", "HS", "DL", "deprecated"), ("AR", "HS", "SU", "deprecated"), ("AR", "HS", "VL", "deprecated"),

    ("FB", "RM", "FD", "deprecated"), ("FB", "SM", "FD", "deprecated"), ("FB", "BF", "FD", "deprecated"),
    ("FB", "SM", "SN", "deprecated"), ("FB", "BF", "SN", "deprecated"),
    ("FB", "PN", "DN", "under_review"),

    ("GS", "LG", "ML", "deprecated"),
    ("GS", "TL", "HR", "deprecated"), ("GS", "TL", "HD", "under_review"),
    ("GS", "SG", "HR", "deprecated"),
    ("GS", "IN", "ML", "under_review"),

    ("ET", "RS", "FN", "under_review"),
    ("ET", "RS", "LC", "deprecated"), ("ET", "RS", "ST", "deprecated"), ("ET", "RS", "GP", "deprecated"),
    ("ET", "TP", "ST", "deprecated"),
    ("ET", "NC", "LC", "under_review"), ("ET", "NC", "ST", "deprecated"),

    ("VP", "TV", "SDS", "under_review"), ("VP", "BV", "SDS", "under_review"),
    ("VP", "TR", "SDS", "deprecated"), ("VP", "UV", "SDS", "deprecated"), ("VP", "GV", "SDS", "deprecated"),
]


def seed_service_taxonomy(db):
    """Seeds service_categories / service_groups / service_subgroups --
    the GLOBAL Service Coding System taxonomy (see schema.sql's MODULE I
    and SERVICE_CATEGORIES/SERVICE_GROUPS/SERVICE_SUBGROUP_SETS/
    SERVICE_SUBGROUP_OVERRIDES above). Idempotent, like seed_global_
    lookups() below -- INSERT OR IGNORE keyed on each table's UNIQUE
    constraint, safe to call on every seed-tenant/migration run."""
    for code, name, description, is_active in SERVICE_CATEGORIES:
        db.execute(
            "INSERT OR IGNORE INTO service_categories (category_code, category_name, description, is_active) "
            "VALUES (?, ?, ?, ?)",
            (code, name, description, 1 if is_active else 0),
        )

    for category_code, group_code, group_name, description in SERVICE_GROUPS:
        db.execute(
            """INSERT OR IGNORE INTO service_groups (category_id, group_code, group_name, description)
               SELECT category_id, ?, ?, ? FROM service_categories WHERE category_code = ?""",
            (group_code, group_name, description, category_code),
        )

    for category_code, group_codes, subgroups in SERVICE_SUBGROUP_SETS:
        for group_code in group_codes:
            for subgroup_code, subgroup_name in subgroups:
                db.execute(
                    """INSERT OR IGNORE INTO service_subgroups (group_id, subgroup_code, subgroup_name)
                       SELECT g.group_id, ?, ?
                       FROM service_groups g JOIN service_categories c ON c.category_id = g.category_id
                       WHERE c.category_code = ? AND g.group_code = ?""",
                    (subgroup_code, subgroup_name, category_code, group_code),
                )

    for category_code, group_code, subgroup_code, status in SERVICE_SUBGROUP_OVERRIDES:
        db.execute(
            """UPDATE service_subgroups SET validity_status = ?
               WHERE subgroup_code = ? AND group_id = (
                   SELECT g.group_id FROM service_groups g
                   JOIN service_categories c ON c.category_id = g.category_id
                   WHERE c.category_code = ? AND g.group_code = ?
               )""",
            (status, subgroup_code, category_code, group_code),
        )

    db.commit()


# Representative demo services for the Heritage Tours tenant -- NOT part of
# seed_service_taxonomy()/seed_lookup_tables() since (like seed_gurdwara_
# pois()/seed_organization_intelligence()) it's Heritage-Tours-specific
# flavor, not something every future tenant should get by default. Drawn
# straight from the worked examples in Service_Coding_System_Schema.md
# (S11.5's Umrah charter, S19.2's composite Umrah invoice, ...) rather than
# invented data, and kept small (a handful, not the full ~200-combination
# taxonomy) -- the point is to prove the module works end-to-end and give
# Zeb a starting example of each shape, not to pre-populate every possible
# code. (category_code, group_code, subgroup_code, description)
DEMO_SERVICES = [
    ("TP", "MN", "MC", "Multi-Nation Heritage Tour: Boston - Kuala Lumpur/Penang - Lahore/Islamabad"),
    ("AT", "CH", "EC", "Umrah group charter flight, Lahore-Jeddah, Economy"),
    ("VP", "UV", "STD", "Saudi Umrah Visa, Standard Processing"),
    ("AR", "HT", "ST", "Standard Room, Makkah hotel, 5 nights"),
    ("GT", "BS", "LD", "Luxury coach with driver, Jeddah-Makkah-Madinah transfers"),
    ("GS", "TL", "ML", "Multi-Day Tour Leader/Escort, combined Malaysia-Pakistan tour"),
    ("ET", "MU", "FN", "Lahore Fort Entry, Foreign National Rate"),
    ("FB", "RM", "DN", "Traditional Pakistani Dinner, Cuckoo's Den, Lahore"),
]


def seed_demo_services(db, tenant_id: int):
    """Seeds DEMO_SERVICES for one tenant. Idempotent: no-ops if this
    tenant already has ANY services rows, same guard style as seed_
    organization_intelligence() above, so re-running seed-tenant (or the
    migration this also backs) never creates duplicates or renumbers
    anything a real user has since added."""
    existing = db.execute("SELECT 1 FROM services WHERE tenant_id = ? LIMIT 1", (tenant_id,)).fetchone()
    if existing:
        return

    for category_code, group_code, subgroup_code, description in DEMO_SERVICES:
        subgroup = db.execute(
            """SELECT sg.subgroup_id FROM service_subgroups sg
               JOIN service_groups g ON g.group_id = sg.group_id
               JOIN service_categories c ON c.category_id = g.category_id
               WHERE c.category_code = ? AND g.group_code = ? AND sg.subgroup_code = ?""",
            (category_code, group_code, subgroup_code),
        ).fetchone()
        if subgroup is None:
            continue  # taxonomy not seeded yet -- shouldn't happen, but never hard-fail demo data
        next_seq = (db.execute(
            "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS n FROM services WHERE tenant_id = ? AND subgroup_id = ?",
            (tenant_id, subgroup["subgroup_id"]),
        ).fetchone())["n"]
        service_code = f"{category_code}-{group_code}-{subgroup_code}-{next_seq:04d}"
        db.execute(
            """INSERT INTO services (tenant_id, subgroup_id, sequence_number, service_code, description, status)
               VALUES (?, ?, ?, ?, ?, 'active')""",
            (tenant_id, subgroup["subgroup_id"], next_seq, service_code, description),
        )
    db.commit()


# ============================================================================
# Product Coding System taxonomy (Inventory Management -> Products
# sub-module) -- see schema.sql's MODULE J. Unlike Services' taxonomy
# (SERVICE_CATEGORIES/SERVICE_GROUPS/SERVICE_SUBGROUP_SETS above, hand-
# assembled from a shared-vocabulary design doc), every value below is
# transcribed 1:1 from TTMS_Products_Seed_Data.csv (166 rows, deduplicated
# on Category/Group/Sub-Group per Claude_Code_Prompt_Products_Module.md's
# explicit instruction: "derive these by deduplicating the CSV's Category/
# Group/Sub-Group columns; do not hand-transcribe them separately, the CSV
# is the single source of truth") -- so PRODUCT_CATEGORIES/PRODUCT_GROUPS/
# PRODUCT_SUBGROUPS/PRODUCTS_SEED_DATA are a straight, mechanical
# transcription of that file, not a curated or restructured version of it.
# Two codes look surprising out of context but are deliberate, per the
# prompt's design notes -- not bugs: Backpacks uses Sub-Group 'BK' (not
# 'BP', the Category code for Beauty Products) and Active Recreation uses
# 'AC' (not 'AR', which would collide with Services' Accommodation Category
# code -- no DB constraint requires avoiding it, it's just clearer to a
# human reading a bare code out of context). BG-TD-CA (Computing
# Accessories) was a labeling inconsistency in the source document -- its
# three sibling Sub-Groups were explicitly labeled "Sub-Group", this one
# wasn't, but it's treated as a 4th Sub-Group for structural consistency.
# ============================================================================

PRODUCT_CATEGORIES = [
    ('BP', 'Beauty Products'),
    ('TA', 'Travel Accessories'),
    ('BG', 'Business Accessories & Sports Gifts'),
]

# (category_code, group_code, group_name)
PRODUCT_GROUPS = [
    ('BP', 'CM', 'Cosmetics & Color Cosmetics (Makeup)'),
    ('BP', 'SK', 'Skincare'),
    ('BP', 'HC', 'Haircare'),
    ('BP', 'FR', 'Fragrances'),
    ('BP', 'PH', 'Personal Care & Hygiene'),
    ('TA', 'TL', 'Travel Gear & Luggage'),
    ('TA', 'PB', 'Personal Bags & Daily Carry'),
    ('TA', 'UT', "Utility Travel Accessories (Traveller's Aids)"),
    ('TA', 'WA', 'Wearable Accessories & Apparel Aids'),
    ('TA', 'SN', 'Specialty & Novelty Gifts'),
    ('TA', 'GB', 'Gift Sets, Baskets & Bundles (Pre-Packaged)'),
    ('BG', 'CS', 'Corporate Stationery & Desk Accessories'),
    ('BG', 'TD', 'Tech Accessories & Digital Gifts'),
    ('BG', 'LD', 'Lifestyle, Dining & Novelty Gifts'),
    ('BG', 'SR', 'Sports & Recreational Gifts'),
]

# (category_code, group_code, subgroup_code, subgroup_name) -- every
# combination seeds with validity_status='active' (the column default);
# this taxonomy has zero deprecated/under-review combinations, per the
# prompt's design notes -- don't invent restrictions the source data
# doesn't have.
PRODUCT_SUBGROUPS = [
    ('BP', 'CM', 'FC', 'Face'),
    ('BP', 'CM', 'EY', 'Eye'),
    ('BP', 'CM', 'LP', 'Lip'),
    ('BP', 'CM', 'NL', 'Nail'),
    ('BP', 'SK', 'FA', 'Facial Care'),
    ('BP', 'SK', 'BC', 'Body Care'),
    ('BP', 'SK', 'SU', 'Sun Care'),
    ('BP', 'HC', 'WR', 'Wash & Rinse'),
    ('BP', 'HC', 'ST', 'Styling'),
    ('BP', 'HC', 'CC', 'Chemical & Color'),
    ('BP', 'HC', 'SC', 'Scalp & Treatment'),
    ('BP', 'FR', 'PF', 'Prestige/Fine Fragrance'),
    ('BP', 'FR', 'DW', 'Daily Wear'),
    ('BP', 'PH', 'BB', 'Bath & Body'),
    ('BP', 'PH', 'DO', 'Deodorants'),
    ('BP', 'PH', 'SG', 'Shaving & Grooming'),
    ('TA', 'TL', 'SR', 'Suitcases & Rollers'),
    ('TA', 'TL', 'CW', 'Carry-Ons & Weekenders'),
    ('TA', 'TL', 'BK', 'Backpacks'),
    ('TA', 'PB', 'SC', 'Shoulder & Crossbody Bags'),
    ('TA', 'PB', 'AB', 'Accessory Bags'),
    ('TA', 'PB', 'SD', 'Security & Document Holders'),
    ('TA', 'UT', 'UR', 'Umbrellas & Rain Gear'),
    ('TA', 'UT', 'SL', 'Security & Locks'),
    ('TA', 'UT', 'CV', 'Comfort & Convenience'),
    ('TA', 'WA', 'SE', 'Sunglasses & Eyewear'),
    ('TA', 'WA', 'HW', 'Headwear'),
    ('TA', 'WA', 'BS', 'Belts & Suspenders'),
    ('TA', 'SN', 'NG', 'Novelty & Gag'),
    ('TA', 'SN', 'SK', 'Souvenir & Keepsake'),
    ('TA', 'SN', 'CL', 'Collectibles'),
    ('TA', 'GB', 'GF', 'Gourmet Food & Beverage'),
    ('TA', 'GB', 'PW', 'Pampering & Wellness'),
    ('TA', 'GB', 'AH', 'Activity & Hobby'),
    ('BG', 'CS', 'WI', 'Writing Instruments'),
    ('BG', 'CS', 'NO', 'Notebooks & Organizers'),
    ('BG', 'CS', 'MD', 'Mailing & Desktop Supplies'),
    ('BG', 'TD', 'CH', 'Computing Hardware'),
    ('BG', 'TD', 'AP', 'Audio Peripherals'),
    ('BG', 'TD', 'DS', 'Data Storage'),
    ('BG', 'TD', 'CA', 'Computing Accessories'),
    ('BG', 'LD', 'BW', 'Beverageware'),
    ('BG', 'LD', 'TG', 'Social & Tabletop Gaming'),
    ('BG', 'SR', 'GE', 'Golf Equipment'),
    ('BG', 'SR', 'AC', 'Active Recreation'),
]


def seed_product_taxonomy(db):
    """Seeds product_categories / product_groups / product_subgroups --
    the GLOBAL Product Coding System taxonomy (see schema.sql's MODULE J
    and PRODUCT_CATEGORIES/PRODUCT_GROUPS/PRODUCT_SUBGROUPS above).
    Idempotent, same INSERT OR IGNORE pattern as seed_service_taxonomy()
    above -- safe to call on every seed-tenant/migration run. A separate,
    independent registry from service_categories/service_groups/service_
    subgroups: nothing here ever reads or writes those tables (see
    schema.sql's MODULE J note and test_products.py's explicit proof)."""
    for code, name in PRODUCT_CATEGORIES:
        db.execute(
            "INSERT OR IGNORE INTO product_categories (category_code, category_name) VALUES (?, ?)",
            (code, name),
        )

    for category_code, group_code, group_name in PRODUCT_GROUPS:
        db.execute(
            """INSERT OR IGNORE INTO product_groups (category_id, group_code, group_name)
               SELECT category_id, ?, ? FROM product_categories WHERE category_code = ?""",
            (group_code, group_name, category_code),
        )

    for category_code, group_code, subgroup_code, subgroup_name in PRODUCT_SUBGROUPS:
        db.execute(
            """INSERT OR IGNORE INTO product_subgroups (group_id, subgroup_code, subgroup_name)
               SELECT g.group_id, ?, ?
               FROM product_groups g JOIN product_categories c ON c.category_id = g.category_id
               WHERE c.category_code = ? AND g.group_code = ?""",
            (subgroup_code, subgroup_name, category_code, group_code),
        )

    db.commit()

# (category_code, group_code, subgroup_code, sequence_number, product_name)
# -- all 166 rows from TTMS_Products_Seed_Data.csv, in file order, using its
# pre-assigned Product Code sequence numbers as-is (not regenerated).
PRODUCTS_SEED_DATA = [
    ('BP', 'CM', 'FC', 1, 'Foundations'),
    ('BP', 'CM', 'FC', 2, 'Concealers'),
    ('BP', 'CM', 'FC', 3, 'Face Powders'),
    ('BP', 'CM', 'FC', 4, 'Primers'),
    ('BP', 'CM', 'FC', 5, 'Blushes'),
    ('BP', 'CM', 'EY', 1, 'Mascara'),
    ('BP', 'CM', 'EY', 2, 'Eyeliners'),
    ('BP', 'CM', 'EY', 3, 'Eyeshadows'),
    ('BP', 'CM', 'EY', 4, 'Eyebrow Pencils'),
    ('BP', 'CM', 'LP', 1, 'Lipsticks'),
    ('BP', 'CM', 'LP', 2, 'Lip Liners'),
    ('BP', 'CM', 'LP', 3, 'Lip Glosses'),
    ('BP', 'CM', 'LP', 4, 'Lip Balms'),
    ('BP', 'CM', 'NL', 1, 'Nail Polishes'),
    ('BP', 'CM', 'NL', 2, 'Base Coats'),
    ('BP', 'CM', 'NL', 3, 'Artificial Nails'),
    ('BP', 'CM', 'NL', 4, 'Nail Polish Removers'),
    ('BP', 'SK', 'FA', 1, 'Cleansers'),
    ('BP', 'SK', 'FA', 2, 'Toners'),
    ('BP', 'SK', 'FA', 3, 'Face Serums'),
    ('BP', 'SK', 'FA', 4, 'Moisturizers'),
    ('BP', 'SK', 'FA', 5, 'Face Masks'),
    ('BP', 'SK', 'BC', 1, 'Hand & Body Lotions'),
    ('BP', 'SK', 'BC', 2, 'Stretch Mark Creams'),
    ('BP', 'SK', 'BC', 3, 'Foot Creams'),
    ('BP', 'SK', 'SU', 1, 'Sunscreen Lotions'),
    ('BP', 'SK', 'SU', 2, 'Sunblocks'),
    ('BP', 'SK', 'SU', 3, 'Sunless Tanning Sprays'),
    ('BP', 'HC', 'WR', 1, 'Shampoos'),
    ('BP', 'HC', 'WR', 2, 'Conditioners'),
    ('BP', 'HC', 'WR', 3, 'Deep Conditioning Masks'),
    ('BP', 'HC', 'ST', 1, 'Hair Sprays'),
    ('BP', 'HC', 'ST', 2, 'Hair Gels'),
    ('BP', 'HC', 'ST', 3, 'Hair Mousses'),
    ('BP', 'HC', 'ST', 4, 'Pomades'),
    ('BP', 'HC', 'ST', 5, 'Heat Protectants'),
    ('BP', 'HC', 'CC', 1, 'Hair Dyes'),
    ('BP', 'HC', 'CC', 2, 'Bleaching Agents'),
    ('BP', 'HC', 'CC', 3, 'Relaxers'),
    ('BP', 'HC', 'CC', 4, 'Perms'),
    ('BP', 'HC', 'SC', 1, 'Hair Loss Prevention Formulas'),
    ('BP', 'HC', 'SC', 2, 'Anti-Dandruff Solutions'),
    ('BP', 'HC', 'SC', 3, 'Scalp Oils'),
    ('BP', 'FR', 'PF', 1, 'Eau de Parfum (EDP)'),
    ('BP', 'FR', 'PF', 2, 'Eau de Toilette (EDT)'),
    ('BP', 'FR', 'DW', 1, 'Body Sprays'),
    ('BP', 'FR', 'DW', 2, 'Colognes'),
    ('BP', 'FR', 'DW', 3, 'Perfumed Body Mists'),
    ('BP', 'PH', 'BB', 1, 'Bar Soaps'),
    ('BP', 'PH', 'BB', 2, 'Body Washes'),
    ('BP', 'PH', 'BB', 3, 'Bubble Baths'),
    ('BP', 'PH', 'BB', 4, 'Shower Gels'),
    ('BP', 'PH', 'DO', 1, 'Antiperspirants'),
    ('BP', 'PH', 'DO', 2, 'Underarm Sticks'),
    ('BP', 'PH', 'DO', 3, 'Roll-Ons'),
    ('BP', 'PH', 'DO', 4, 'Deodorant Body Wipes'),
    ('BP', 'PH', 'SG', 1, 'Razors'),
    ('BP', 'PH', 'SG', 2, 'Shaving Creams'),
    ('BP', 'PH', 'SG', 3, 'Aftershaves'),
    ('BP', 'PH', 'SG', 4, 'Depilatories'),
    ('TA', 'TL', 'SR', 1, 'Hardside Spinner Suitcases'),
    ('TA', 'TL', 'SR', 2, 'Softside Upright Suitcases'),
    ('TA', 'TL', 'SR', 3, 'Expandable Checked Luggage'),
    ('TA', 'TL', 'CW', 1, 'Flight-Approved Carry-On Bags'),
    ('TA', 'TL', 'CW', 2, 'Duffel Bags'),
    ('TA', 'TL', 'CW', 3, 'Garment Bags'),
    ('TA', 'TL', 'CW', 4, 'Overnight Weekend Bags'),
    ('TA', 'TL', 'BK', 1, 'Travel Backpacks'),
    ('TA', 'TL', 'BK', 2, 'Laptop Backpacks'),
    ('TA', 'TL', 'BK', 3, 'Hiking Packs'),
    ('TA', 'TL', 'BK', 4, 'Anti-Theft Daypacks'),
    ('TA', 'PB', 'SC', 1, 'Shoulder Bags'),
    ('TA', 'PB', 'SC', 2, 'Messenger Bags'),
    ('TA', 'PB', 'SC', 3, 'Sling Bags'),
    ('TA', 'PB', 'SC', 4, 'Hands-Free Belt Bags (Fanny Packs)'),
    ('TA', 'PB', 'AB', 1, 'Toiletry Bags (Dopp Kits)'),
    ('TA', 'PB', 'AB', 2, 'Cosmetic Cases'),
    ('TA', 'PB', 'AB', 3, 'Wet/Dry Pouches'),
    ('TA', 'PB', 'AB', 4, 'Tech Organizers for Cables'),
    ('TA', 'PB', 'SD', 1, 'Neck Pouches'),
    ('TA', 'PB', 'SD', 2, 'Money Belts'),
    ('TA', 'PB', 'SD', 3, 'RFID-Blocking Passport Wallets'),
    ('TA', 'PB', 'SD', 4, 'Hidden Travel Pouches'),
    ('TA', 'UT', 'UR', 1, 'Compact Travel Umbrellas'),
    ('TA', 'UT', 'UR', 2, 'Windproof Umbrellas'),
    ('TA', 'UT', 'UR', 3, 'Ponchos'),
    ('TA', 'UT', 'UR', 4, 'Packable Rain Jackets'),
    ('TA', 'UT', 'SL', 1, 'TSA-Approved Luggage Locks (Combination & Key)'),
    ('TA', 'UT', 'SL', 2, 'Luggage Straps'),
    ('TA', 'UT', 'SL', 3, 'Smart Bluetooth Baggage Trackers'),
    ('TA', 'UT', 'CV', 1, 'Travel Pillows'),
    ('TA', 'UT', 'CV', 2, 'Eye Masks'),
    ('TA', 'UT', 'CV', 3, 'Earplugs'),
    ('TA', 'UT', 'CV', 4, 'Luggage Tags'),
    ('TA', 'UT', 'CV', 5, 'Portable Luggage Scales'),
    ('TA', 'WA', 'SE', 1, 'Polarized Sunglasses'),
    ('TA', 'WA', 'SE', 2, 'Folding Sunglasses'),
    ('TA', 'WA', 'SE', 3, 'Protective Eyewear Cases'),
    ('TA', 'WA', 'SE', 4, 'Sunglass Straps'),
    ('TA', 'WA', 'HW', 1, 'Packable Sun Hats'),
    ('TA', 'WA', 'HW', 2, 'Baseball Caps'),
    ('TA', 'WA', 'HW', 3, 'Beanies'),
    ('TA', 'WA', 'HW', 4, 'Visors'),
    ('TA', 'WA', 'BS', 1, 'Travel Belts (Metal-Free)'),
    ('TA', 'WA', 'BS', 2, 'Standard Utility Belts'),
    ('TA', 'SN', 'NG', 1, 'Humorous Desk Signs'),
    ('TA', 'SN', 'NG', 2, 'Quirky Coffee Mugs'),
    ('TA', 'SN', 'NG', 3, 'Eccentric Stress-Relief Items'),
    ('TA', 'SN', 'SK', 1, 'Travel Magnets'),
    ('TA', 'SN', 'SK', 2, 'Collector Spoons'),
    ('TA', 'SN', 'SK', 3, 'Custom State/City Keychains'),
    ('TA', 'SN', 'SK', 4, 'Photo Snow Globes'),
    ('TA', 'SN', 'CL', 1, 'Limited-Edition Figurines'),
    ('TA', 'SN', 'CL', 2, 'Pop-Culture Statues'),
    ('TA', 'SN', 'CL', 3, 'Commemorative Coins'),
    ('TA', 'GB', 'GF', 1, 'Fruit Baskets'),
    ('TA', 'GB', 'GF', 2, 'Chocolate Boxes'),
    ('TA', 'GB', 'GF', 3, 'Charcuterie Arrangements'),
    ('TA', 'GB', 'GF', 4, 'Artisanal Tea/Coffee Samplers'),
    ('TA', 'GB', 'PW', 1, 'Bath Bomb Multi-Packs'),
    ('TA', 'GB', 'PW', 2, 'Home Spa Bundles'),
    ('TA', 'GB', 'PW', 3, 'Curated Essential Oil Kits'),
    ('TA', 'GB', 'AH', 1, 'Beginner Painting Sets'),
    ('TA', 'GB', 'AH', 2, 'DIY Craft Bundles'),
    ('TA', 'GB', 'AH', 3, 'Multi-Piece Board Game Gift Packs'),
    ('BG', 'CS', 'WI', 1, 'Premium Ballpoint Pens'),
    ('BG', 'CS', 'WI', 2, 'Rollerball Pens'),
    ('BG', 'CS', 'WI', 3, 'Fountain Pens'),
    ('BG', 'CS', 'WI', 4, 'Mechanical Pencil Gift Sets'),
    ('BG', 'CS', 'NO', 1, 'Bound Diaries'),
    ('BG', 'CS', 'NO', 2, 'Executive Notebooks'),
    ('BG', 'CS', 'NO', 3, 'Legal Writing Pads'),
    ('BG', 'CS', 'NO', 4, 'Leather Portfolio Covers'),
    ('BG', 'CS', 'MD', 1, 'Paper Envelopes'),
    ('BG', 'CS', 'MD', 2, 'Document Folders'),
    ('BG', 'CS', 'MD', 3, 'Post Cards'),
    ('BG', 'CS', 'MD', 4, 'Desk Organizers'),
    ('BG', 'CS', 'MD', 5, 'Paperweights'),
    ('BG', 'TD', 'CH', 1, 'Business Laptops'),
    ('BG', 'TD', 'CH', 2, 'Tablets'),
    ('BG', 'TD', 'CH', 3, 'E-Readers'),
    ('BG', 'TD', 'AP', 1, 'Wireless Earphones'),
    ('BG', 'TD', 'AP', 2, 'Noise-Canceling Headphones'),
    ('BG', 'TD', 'AP', 3, 'Portable Bluetooth Speakers'),
    ('BG', 'TD', 'DS', 1, 'USB Flash Memory Sticks'),
    ('BG', 'TD', 'DS', 2, 'External Hard Drives'),
    ('BG', 'TD', 'DS', 3, 'Memory Card Readers'),
    ('BG', 'TD', 'CA', 1, 'Mouse Pads'),
    ('BG', 'TD', 'CA', 2, 'Mouse Pointers'),
    ('BG', 'TD', 'CA', 3, 'Laser Pointers'),
    ('BG', 'TD', 'CA', 4, 'Power Cables'),
    ('BG', 'TD', 'CA', 5, 'USB Cables'),
    ('BG', 'TD', 'CA', 6, 'USB Chargers'),
    ('BG', 'LD', 'BW', 1, 'Ceramic Coffee Mugs'),
    ('BG', 'LD', 'BW', 2, 'Insulated Travel Tumblers'),
    ('BG', 'LD', 'BW', 3, 'Stainless Steel Water Bottles'),
    ('BG', 'LD', 'TG', 1, 'Premium Playing Cards'),
    ('BG', 'LD', 'TG', 2, 'Custom Poker Chip Sets'),
    ('BG', 'LD', 'TG', 3, 'Travel Board Games'),
    ('BG', 'SR', 'GE', 1, 'Golf Balls'),
    ('BG', 'SR', 'GE', 2, 'Custom Golf Tees'),
    ('BG', 'SR', 'GE', 3, 'Divot Repair Tools'),
    ('BG', 'SR', 'GE', 4, 'Golf Towels'),
    ('BG', 'SR', 'AC', 1, 'Sports Water Bottles'),
    ('BG', 'SR', 'AC', 2, 'Fitness Trackers'),
    ('BG', 'SR', 'AC', 3, 'Gym Towels'),
]


def seed_product_catalog(db, tenant_id: int):
    """Seeds the full PRODUCTS_SEED_DATA (all 166 rows) for one tenant --
    the Master Product Inventory table, per the Products prompt's Part 2.3
    ("Populate the Master Product Inventory by loading all 166 rows...
    using the CSV's pre-assigned Product Code values as-is"). Unlike
    seed_demo_services() above (a curated handful of worked examples),
    this seeds the COMPLETE taxonomy-sourced catalog, since the prompt's
    acceptance criteria requires exactly 166 rows in `products`, not a
    representative sample.

    Idempotent: no-ops if this tenant already has ANY products rows, same
    guard style as seed_demo_services(), so re-running seed-tenant (or the
    migration this also backs) never duplicates or interferes with a real
    user's own catalog edits. sku/price/cost/stock_quantity are left
    null/zero-valued placeholders and status defaults to 'draft' (per the
    prompt: "this seed data is taxonomy-sourced, not a real price list") --
    each row needs review/pricing before a tenant would actually sell
    against it, so it's honest to not seed it as 'active'."""
    existing = db.execute("SELECT 1 FROM products WHERE tenant_id = ? LIMIT 1", (tenant_id,)).fetchone()
    if existing:
        return

    for category_code, group_code, subgroup_code, sequence_number, product_name in PRODUCTS_SEED_DATA:
        subgroup = db.execute(
            """SELECT sg.subgroup_id FROM product_subgroups sg
               JOIN product_groups g ON g.group_id = sg.group_id
               JOIN product_categories c ON c.category_id = g.category_id
               WHERE c.category_code = ? AND g.group_code = ? AND sg.subgroup_code = ?""",
            (category_code, group_code, subgroup_code),
        ).fetchone()
        if subgroup is None:
            continue  # taxonomy not seeded yet -- shouldn't happen, but never hard-fail seed data
        product_code = f"{category_code}-{group_code}-{subgroup_code}-{sequence_number:04d}"
        db.execute(
            """INSERT INTO products (tenant_id, subgroup_id, sequence_number, product_code, product_name, status)
               VALUES (?, ?, ?, ?, ?, 'draft')""",
            (tenant_id, subgroup["subgroup_id"], sequence_number, product_code, product_name),
        )
    db.commit()


def seed_global_lookups(db):
    """Seeds regions, countries, states, cities, and country_phone_codes —
    the GLOBAL (non-tenant-scoped) lookup tables, shared by every tenant.
    Safe to re-run for every new tenant; INSERT OR IGNORE (or an equivalent
    manual existence check, for tables with no natural unique code) makes
    it a no-op once already populated."""
    for i, (code, label) in enumerate(REGIONS):
        db.execute(
            "INSERT OR IGNORE INTO regions (code, label, sort_order, is_active) VALUES (?, ?, ?, 1)",
            (code, label, i),
        )

    for i, (code, label) in enumerate(COUNTRIES):
        db.execute(
            "INSERT OR IGNORE INTO countries (code, label, sort_order, is_active) VALUES (?, ?, ?, 1)",
            (code, label, i),
        )

    # Assign each country's region_id (idempotent — always safe to re-set).
    for code, region_code in COUNTRY_REGIONS.items():
        db.execute(
            "UPDATE countries SET region_id = (SELECT region_id FROM regions WHERE code = ?) "
            "WHERE code = ?",
            (region_code, code),
        )

    _seed_states(db, "US", US_STATES)
    _seed_states(db, "CA", CANADA_PROVINCES)
    seed_pakistan_geography(db)

    for code, calling_code in COUNTRY_CALLING_CODES.items():
        row = db.execute("SELECT country_id FROM countries WHERE code = ?", (code,)).fetchone()
        if row:
            db.execute(
                "INSERT OR IGNORE INTO country_phone_codes (country_id, calling_code, label, is_active) "
                "SELECT ?, ?, ?, 1 WHERE NOT EXISTS ("
                "  SELECT 1 FROM country_phone_codes WHERE country_id = ? AND calling_code = ?"
                ")",
                (row["country_id"], calling_code, f"{code} {calling_code}", row["country_id"], calling_code),
            )

    seed_service_taxonomy(db)
    seed_product_taxonomy(db)

    db.commit()


def _seed_host_address_types(db, tenant_id: int):
    """Seeds host_address_types for one tenant, same shape as _seed_simple()
    but additionally flags the "Head Office" row is_head_office=1 — that
    flag, not the label text, is what the hard "only one Head Office
    address" rule (blueprints/system_mgmt.py) checks, so it survives a
    later relabel. Safe to re-run: INSERT OR IGNORE keyed on (tenant_id,
    code), same as _seed_simple()."""
    for i, label in enumerate(HOST_ADDRESS_TYPES):
        code = _slug(label)
        db.execute(
            "INSERT OR IGNORE INTO host_address_types (tenant_id, code, label, sort_order, is_active, is_head_office) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (tenant_id, code, label, i, 1 if label == "Head Office" else 0),
        )


def seed_host_organization(db, tenant_id: int):
    """Auto-provisions the ONE host_organizations row every tenant needs
    (see schema.sql MODULE H) — named after the tenant itself as a
    reasonable starting point, editable immediately from System
    Management's "Host Organization" box. Also seeds
    host_address_types/host_phone_types for the tenant, same as any other
    Module E-shaped lookup.

    Generic infrastructure needed by the HR module itself (an Employee's
    Office Work Address picker has nothing to point at otherwise) — NOT
    Heritage-Tours-specific demo data — so this is called from
    seed_lookup_tables() below, for every tenant, unlike e.g.
    seed_gurdwara_pois()/seed_organization_intelligence() which are
    Heritage Tours-only.

    Idempotent: does nothing if this tenant already has a host_organizations
    row (UNIQUE(tenant_id) in schema.sql also guards this at the DB level)."""
    existing = db.execute(
        "SELECT host_organization_id FROM host_organizations WHERE tenant_id = ?", (tenant_id,)
    ).fetchone()
    if not existing:
        tenant = db.execute("SELECT tenant_name FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
        db.execute(
            "INSERT INTO host_organizations (tenant_id, organization_name) VALUES (?, ?)",
            (tenant_id, tenant["tenant_name"] if tenant else "My Organization"),
        )
    _seed_host_address_types(db, tenant_id)
    _seed_simple(db, "host_phone_types", tenant_id, HOST_PHONE_TYPES)


def seed_lookup_tables(db, tenant_id: int):
    """Seeds every TENANT-SCOPED lookup table (Module E, minus the 3 global
    ones) with the same PROPOSED starter values PIMS shipped with, for one
    tenant — plus the global geography tables, which only actually insert
    once no matter how many tenants call this."""
    _seed_simple(db, "contact_categories", tenant_id, CONTACT_CATEGORIES)
    _seed_simple(db, "contact_titles", tenant_id, CONTACT_TITLES)
    _seed_simple(db, "contact_suffixes", tenant_id, CONTACT_SUFFIXES)
    _seed_simple(db, "professions", tenant_id, PROFESSIONS)
    _seed_simple(db, "contact_contexts", tenant_id, CONTACT_CONTEXTS)
    _seed_simple(db, "organization_types", tenant_id, ORGANIZATION_TYPES)
    _seed_simple(db, "poi_types", tenant_id, POI_TYPES)
    _seed_simple(db, "knowledge_domains", tenant_id, KNOWLEDGE_DOMAINS)
    _seed_simple(db, "content_types", tenant_id, CONTENT_TYPES)
    _seed_simple(db, "supplier_address_types", tenant_id, SUPPLIER_ADDRESS_TYPES)
    _seed_simple(db, "organization_address_types", tenant_id, ORGANIZATION_ADDRESS_TYPES)
    _seed_simple(db, "organization_phone_types", tenant_id, ORGANIZATION_PHONE_TYPES)
    _seed_simple(db, "phone_types", tenant_id, SUPPLIER_PHONE_TYPES)
    seed_supplier_lookups(db, tenant_id)

    # Human Resources (Human_Resource_Module1a.docx) — all generic starter
    # lookups + the tenant's one auto-provisioned Host Organization record.
    seed_host_organization(db, tenant_id)
    _seed_simple(db, "genders", tenant_id, GENDERS)
    _seed_simple(db, "employee_types", tenant_id, EMPLOYEE_TYPES)
    _seed_simple(db, "departments", tenant_id, DEPARTMENTS)
    _seed_simple(db, "job_titles", tenant_id, JOB_TITLES)
    _seed_simple(db, "employee_phone_types", tenant_id, EMPLOYEE_PHONE_TYPES)
    _seed_simple(db, "resource_types", tenant_id, RESOURCE_TYPES)
    _seed_simple(db, "hr_roles", tenant_id, HR_ROLES)

    seed_global_lookups(db)
    db.commit()


def seed_first_tenant(db):
    """Phase 1.1 initial data: the first tenant (Heritage Tours) with its
    Tenant Admin (username 'Zeb', password 'Zebra' — the person running this
    should change it after first login), its own set of lookup tables, and
    the shared global geography tables. Safe to re-run: does nothing if
    Heritage Tours already exists.

    Called by `flask --app app seed-tenant`. This is how Phase 1.1 gets its
    first working login without anyone needing to go through the /setup
    wizard (which still exists, for provisioning additional tenants later).
    """
    from security import crypto
    from security.passwords import hash_password
    from security.wordlist import generate_seed_phrase, hash_phrase

    existing = db.execute("SELECT tenant_id FROM tenants WHERE tenant_code = ?", ("HERITAGE",)).fetchone()
    if existing:
        return existing["tenant_id"]

    dek = crypto.new_tenant_dek()
    dek_wrapped = crypto.wrap_tenant_dek(dek)
    cur = db.execute(
        "INSERT INTO tenants (tenant_code, tenant_name, dek_wrapped) VALUES (?, ?, ?)",
        ("HERITAGE", "Heritage Tours", dek_wrapped),
    )
    tenant_id = cur.lastrowid

    # Recovery seed phrase is generated but not surfaced anywhere for this
    # CLI-seeded admin (there's no UI moment to show it, unlike the /setup
    # wizard's one-time reveal page) — Zeb can set one up later via a
    # proper "generate my recovery phrase" flow once that's built, or via
    # `flask --app app forgot-password` in the meantime. Recorded as a hash
    # only, same as everywhere else; the plaintext is discarded immediately.
    seed_phrase = generate_seed_phrase()
    db.execute(
        """INSERT INTO users (tenant_id, username, display_name, password_hash, role, recovery_seed_hash)
           VALUES (?, 'Zeb', 'Zeb', ?, 'TenantAdmin', ?)""",
        (tenant_id, hash_password("Zebra"), hash_phrase(seed_phrase)),
    )
    db.commit()

    seed_lookup_tables(db, tenant_id)

    # Heritage-Tours-specific demo data (not part of seed_lookup_tables()
    # since it shouldn't be seeded for every future tenant): the 149 Sikh
    # Gurdwara points of interest from LIST OF ALL SIKH GURDWARAS IN
    # PAKISTAN1a.docx. Requires seed_lookup_tables() above to have already
    # created 'Sikh Gurdwara' in poi_types and the Pakistan geography rows.
    seed_gurdwara_pois(db, tenant_id)

    # "Sikh Pilgrimage Sector" AI Agent / Data Enrichment pilot (Nankana
    # Sahib) -- also Heritage-Tours-specific pilot data, same reasoning as
    # seed_gurdwara_pois() above. Must run after it (backfills coordinates
    # onto the Gurdwara rows it just created) and after seed_lookup_tables()
    # above (needs the new Airport/Railway Station/Hospital/Police Station
    # poi_types and Tour Guide supplier_type it seeded).
    seed_nankana_sahib_pilot(db, tenant_id)

    # Sample Organization Intelligence entries -- also Heritage-Tours-
    # specific demo data, same reasoning as seed_gurdwara_pois() above.
    seed_organization_intelligence(db, tenant_id)

    # A handful of representative Services (Inventory Management -> Services
    # sub-module) -- also Heritage-Tours-specific demo data, same reasoning
    # as seed_gurdwara_pois()/seed_organization_intelligence() above.
    # Requires seed_global_lookups() (called inside seed_lookup_tables() ->
    # seed_global_lookups() above) to have already seeded the Service
    # Coding System taxonomy.
    seed_demo_services(db, tenant_id)

    # The full 166-row Product catalog (Inventory Management -> Products
    # sub-module), unlike DEMO_SERVICES above this is the COMPLETE seed
    # set (not a curated handful) per the Products prompt's acceptance
    # criteria -- "product table contains exactly 166 seeded rows".
    # Requires seed_global_lookups() to have already seeded the Product
    # Coding System taxonomy (seed_product_taxonomy(), called alongside
    # seed_service_taxonomy() above).
    seed_product_catalog(db, tenant_id)

    db.execute(
        "INSERT INTO audit_log (tenant_id, action, entity_type, entity_id, detail) VALUES (?, 'Setup', 'tenants', ?, ?)",
        (tenant_id, tenant_id, "Seeded Heritage Tours tenant with admin user 'Zeb' via seed-tenant CLI command"),
    )
    db.commit()
    return tenant_id
