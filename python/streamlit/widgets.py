# Streamlit Basics
# Use the below command to run the streamlit app:
# python -m streamlit run app.py

import streamlit as st

st.title("Streamlit Widgets")
name = st.text_input("Enter your name")
age = st.slider("Select your age", 0, 100, 25)
gender = st.selectbox("Select your gender", ["Male", "Female", "Other"])
hobbies = st.multiselect("Select your hobbies", ["Reading", "Traveling", "Cooking", "Sports"])
if st.button("Submit"):
    st.write(f"Name: {name}")
    st.write(f"Age: {age}")
    st.write(f"Gender: {gender}")
    st.write(f"Hobbies: {', '.join(hobbies)}")
    st.write("Form submitted successfully!")
    
    if age < 18:
        st.warning("You are not eligible to vote.")
    else:
        st.success("You are eligible to vote.")
        
    if gender == "Male":
        st.write("You are a male.")
    else:
        st.write("You are not a male.")
        
    if "Reading" in hobbies:
        st.write("You like reading.")
    else:
        st.write("You don't like reading.")
        
    if "Traveling" in hobbies:
        st.write("You like traveling.")
    else:
        st.write("You don't like traveling.")
        
    if "Cooking" in hobbies:
        st.write("You like cooking.")
    else:
        st.write("You don't like cooking.")
        
    if "Sports" in hobbies:
        st.write("You like sports.")
    else:
        st.write("You don't like sports.")